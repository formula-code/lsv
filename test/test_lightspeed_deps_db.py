"""A changed compiled source with no dependency rows is not skipped."""

import pytest

from asv.contrib.lightspeed.deps_db import BenchmarkId, LightspeedDB


@pytest.fixture
def make_db(tmp_path):
    dbs = []

    def make(deps):
        db = LightspeedDB(str(tmp_path / f"deps{len(dbs)}.db"))
        dbs.append(db)
        for bid, files in deps.items():
            db.store_deps_batch(BenchmarkId(bid), {f: ([1], "sha") for f in files})
        return db
    yield make
    for db in dbs:
        db.close()


def _selected(db, changed):
    return {str(b) for b in db.get_affected_benchmark_ids({f: ([2], "new") for f in changed})}


def test_changed_compiled_file_without_rows_selects_extension_benchmarks(make_db, capsys):
    db = make_db({"b_ext": ["/r/src/a.c", "/r/pkg/x.py"], "b_py": ["/r/pkg/x.py"]})
    assert _selected(db, ["/r/src/new.cpp"]) == {"b_ext"}
    assert "/r/src/new.cpp has no dependency rows; selecting 1 benchmarks" in "".join(capsys.readouterr())


def test_changed_compiled_file_without_rows_selects_all_when_no_extension_rows(make_db):
    db = make_db({"b1": ["/r/pkg/x.py"], "b2": ["/r/pkg/y.py"]})
    db.set_always_affected(BenchmarkId("b3"), "setup_error")
    assert _selected(db, ["/r/src/kernel.rs"]) == {"b1", "b2", "b3"}


def test_changed_python_file_without_rows_selects_nothing(make_db):
    db = make_db({"b1": ["/r/src/a.c"]})
    assert _selected(db, ["/r/pkg/unused.py"]) == set()
