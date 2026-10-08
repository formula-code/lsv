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
    for name in [m for m in sys.modules if m in ("pkg", "benchmarks") or m.startswith(("pkg.", "benchmarks."))]:
        del sys.modules[name]


def test_only_executed_files_are_deps(project):
    ok, reason, deps = survey_one(str(project / "benchmarks"), BenchmarkId("bench_pkg.time_work"), str(project / "pkg"))
    assert ok, reason
    assert {p.split("/pkg/")[-1] for p in deps} == {"used.py"}


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
