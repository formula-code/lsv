"""survey_one records only the source files a benchmark's timed body executes."""

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
    for name in [m for m in sys.modules if m == "pkg" or m.startswith(("pkg.", "bench_pkg"))]:
        del sys.modules[name]


def test_only_executed_files_are_deps(project):
    ok, reason, deps = survey_one(str(project / "benchmarks"), BenchmarkId("bench_pkg.time_work"), str(project / "pkg"))
    assert ok, reason
    assert {p.split("/pkg/")[-1] for p in deps} == {"used.py"}
