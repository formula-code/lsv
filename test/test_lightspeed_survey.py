"""survey_one records only the source files a benchmark's timed body executes."""

import os
import sys
import textwrap

import pytest

pytest.importorskip("coverage")
from asv.contrib.lightspeed.deps_db import BenchmarkId  # noqa: E402
from asv.contrib.lightspeed.survey import survey_one  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "used.py").write_text("def work(n):\n    return sum(range(n))\n")
    (pkg / "unused.py").write_text("def idle():\n    return 0\n")
    bench = tmp_path / "benchmarks"
    bench.mkdir()
    (bench / "__init__.py").write_text("")
    (bench / "bench_pkg.py").write_text(textwrap.dedent("""
        import pkg.unused
        from pkg.used import work

        def time_work():
            work(1000)
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for name in [m for m in sys.modules if m in ("pkg", "benchmarks") or m.startswith(("pkg.", "benchmarks.", "bench_"))]:
        del sys.modules[name]


def test_only_executed_files_are_deps(project):
    ok, reason, deps = survey_one(str(project / "benchmarks"), BenchmarkId("bench_pkg.time_work"), str(project / "pkg"))
    assert ok, reason
    assert {p.split("/pkg/")[-1] for p in deps} == {"used.py"}


def test_failing_teardown_after_a_failing_body_is_recorded(project):
    (project / "benchmarks" / "bench_fail.py").write_text(textwrap.dedent("""
        import pathlib
        calls = pathlib.Path(__file__).with_name("teardowns.txt")

        def time_boom():
            raise RuntimeError("body")

        def teardown():
            calls.write_text(calls.read_text() + "x" if calls.exists() else "x")
            raise TimeoutError("teardown")

        time_boom.teardown = teardown
    """))
    ok, reason, deps = survey_one(str(project / "benchmarks"), BenchmarkId("bench_fail.time_boom"), str(project / "pkg"))
    assert (ok, deps) == (False, {}) and reason.startswith("runtime_error: body")
    assert (project / "benchmarks" / "teardowns.txt").read_text() == "x"


def test_child_process_files_are_deps(project):
    (project / "pkg" / "child.py").write_text("VALUE = sum(range(10))\n")
    (project / "benchmarks" / "bench_child.py").write_text(textwrap.dedent(f"""
        import subprocess
        import sys

        def time_child_import():
            subprocess.run(f"{{sys.executable}} -c 'import pkg.child'", shell=True, check=True, cwd={str(project)!r})
    """))
    env_before = dict(os.environ)
    ok, reason, deps = survey_one(str(project / "benchmarks"), BenchmarkId("bench_child.time_child_import"), str(project / "pkg"))
    assert ok, reason
    assert "child.py" in {p.split("/pkg/")[-1] for p in deps}
    assert dict(os.environ) == env_before


def test_extension_scan_survives_import_during_scan(tmp_path, monkeypatch):
    from types import ModuleType

    from asv.contrib.lightspeed.survey import _add_extension_deps

    class Lazy(ModuleType):
        def __getattr__(self, name):
            sys.modules.setdefault("lazy_loaded_child", ModuleType("lazy_loaded_child"))
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "lazy_parent", Lazy("lazy_parent"))
    monkeypatch.delitem(sys.modules, "lazy_loaded_child", raising=False)
    _add_extension_deps({}, str(tmp_path))


@pytest.fixture
def ext_module(monkeypatch):
    from types import ModuleType

    def load(name, path="/site/x.cpython-311-x86_64-linux-gnu.so"):
        mod = ModuleType(name)
        mod.__file__ = path
        monkeypatch.setitem(sys.modules, name, mod)
    return load


def _rel(deps, root):
    return {os.path.relpath(p, root) for p in deps}


def test_compiled_sources_outside_the_package_folder(tmp_path, ext_module):
    (tmp_path / "pyproject.toml").write_text("")
    for f in ["lib/pkg/__init__.py", "src/impl.h", "src/impl.cc", "src/core.hpp", "src/kernel.rs",
              "src/loops.c.src", "src/defs.pxi", "src/cuda/k.cu", "build/gen.c", "benchmarks/helper.c",
              "src/notes.txt"]:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("int f(void) { return 0; }\n")
    from asv.contrib.lightspeed.survey import _add_extension_deps, scan_build

    ext_module("pkg._ext")
    deps = {}
    _add_extension_deps(deps, str(tmp_path / "lib" / "pkg"),
                        scan_build(str(tmp_path / "lib" / "pkg"), str(tmp_path / "benchmarks")))
    assert _rel(deps, tmp_path) == {"src/impl.h", "src/impl.cc", "src/core.hpp", "src/kernel.rs",
                                    "src/loops.c.src", "src/defs.pxi", "src/cuda/k.cu"}


def test_python_files_compiled_by_cython(tmp_path, ext_module):
    from asv.contrib.lightspeed.survey import _add_extension_deps, scan_build

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "setup.py").write_text("cythonize(['pkg/fast.py'], language_level=3)\nscripts=['pkg/tool.py']\n")
    (pkg / "meson.build").write_text(
        "run_command('cp', 'staged.py', 'staged.pyx')\npy.extension_module('staged', 'staged.pyx')\n"
        "py.install_sources(['plain.py'])\n")
    for f in ["__init__.py", "fast.py", "tool.py", "staged.py", "plain.py", "mypyc_mod.py"]:
        (pkg / f).write_text("def f():\n    return 1\n")
    compiled, _ = scan_build(str(pkg))
    assert _rel(compiled, tmp_path) == {"pkg/fast.py", "pkg/staged.py"}

    ext_module("pkg.mypyc_mod")
    deps = {}
    _add_extension_deps(deps, str(pkg), scan_build(str(pkg)))
    assert _rel(deps, tmp_path) == {"pkg/fast.py", "pkg/staged.py", "pkg/mypyc_mod.py"}


def test_no_extension_loaded_adds_nothing(tmp_path, monkeypatch):
    from asv.contrib.lightspeed.survey import _add_extension_deps

    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "impl.c").write_text("int x;\n")
    for name, mod in list(sys.modules.items()):
        if (getattr(mod, "__file__", None) or "").endswith((".so", ".pyd")):
            monkeypatch.delitem(sys.modules, name)
    deps = {}
    _add_extension_deps(deps, str(tmp_path))
    assert deps == {}


def test_threads_started_in_setup_are_traced(project):
    (project / "pkg" / "worker.py").write_text("def work(n):\n    return sum(range(n))\n")
    (project / "pkg" / "prepare.py").write_text("def prepare():\n    return 1\n")
    (project / "benchmarks" / "bench_thread.py").write_text(textwrap.dedent("""
        import queue
        import threading

        from pkg.prepare import prepare
        from pkg.worker import work

        jobs, done = queue.Queue(), queue.Queue()

        def serve():
            while True:
                n = jobs.get()
                if n is None:
                    return
                done.put(work(n))

        def setup():
            prepare()
            threading.Thread(target=serve, daemon=True).start()

        def teardown():
            jobs.put(None)

        def time_request():
            jobs.put(1000)
            done.get()

        time_request.setup = setup
        time_request.teardown = teardown
    """))
    ok, reason, deps = survey_one(str(project / "benchmarks"), BenchmarkId("bench_thread.time_request"), str(project / "pkg"))
    assert ok, reason
    assert {p.split("/pkg/")[-1] for p in deps} == {"worker.py"}
