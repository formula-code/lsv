"""
Coverage survey: run each benchmark function once (not timed) under
Coverage.py to determine which source files it touches.

Failure modes handled (as per coverage.py's known limitations):
  - execv: process replacement, subprocess exits without writing data
  - _thread: low-level threads not traced, static pre-check
  - sys.settrace: coverage tracer clobbered, runtime detection
  - sys.setprofile: profiler installed, runtime detection

Any failure marks the benchmark as always_affected in the DB.
"""

import ast
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import coverage
from asv_runner._aux import update_sys_path
from asv_runner.discovery import get_benchmark_from_name

from .deps_db import BenchmarkId, LightspeedDB
from .fingerprint import coverage_fingerprint


# --------------------------------------------------------------------------- #
# Low-level thread pre-check                                                   #
# --------------------------------------------------------------------------- #

def _uses_low_level_thread(path: str) -> bool:
    """
    Return True if *path* contains an import of the low-level `_thread`
    module.  This is a static check; dynamic imports are caught at runtime.
    """
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=path)
    except Exception:
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("_thread", "thread"):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module in ("_thread", "thread"):
                return True
    return False


# --------------------------------------------------------------------------- #
# Single-benchmark survey                                                       #
# --------------------------------------------------------------------------- #

SurveyResult = Tuple[
    # success, failure reason / None on success, filename -> (fingerprint, fsha)
    bool,
    Optional[str],
    Dict[str, Tuple[list, str]],
]


def survey_one(
    benchmark_dir: str,
    bid: BenchmarkId,
    source_root: str,
) -> SurveyResult:
    """
    Run `bid` once using Coverage.py and return the files it touches.

    Returns ``(success, failure_reason, file_deps)`` where ``file_deps``
    maps absolute filename to ``(coverage_fingerprint, fsha)``.
    """
    update_sys_path(benchmark_dir)

    asv_name = str(bid)  # "name-paramidx" or just "name"

    try:
        bench = get_benchmark_from_name(benchmark_dir, asv_name)
    except Exception as exc:
        return False, f"load_error: {exc}", {}

    try:
        bench_file = sys.modules[bench.func.__module__].__file__ or ""
    except Exception:
        bench_file = ""

    if bench_file and _uses_low_level_thread(bench_file):
        return False, "_thread import detected (static)", {}

    cov = coverage.Coverage(
        source=[source_root],
        omit=["*/site-packages/*", "*/dist-packages/*"],
        branch=False,
        data_file=None,     # in-memory only
    )

    skip = False
    try:
        skip = bench.do_setup()
    except Exception as exc:
        return False, f"setup_error: {exc}", {}

    if skip:
        return False, "benchmark_skipped", {}

    tracer_before = sys.gettrace()
    profiler_before = sys.getprofile()

    try:
        cov.start()
        try:
            bench.func(*bench._current_params)
        finally:
            cov.stop()
    except Exception as exc:
        bench.do_teardown()
        return False, f"runtime_error: {exc}", {}
    finally:
        try:
            bench.do_teardown()
        except Exception:
            pass

    tracer_after = sys.gettrace()
    profiler_after = sys.getprofile()

    if tracer_after is not tracer_before:
        return False, "sys.settrace clobbered", {}

    if profiler_after is not profiler_before:
        return False, "sys.setprofile clobbered", {}

    try:
        cov_data = cov.get_data()
        measured = cov_data.measured_files()
    except Exception as exc:
        return False, f"coverage_read_error: {exc}", {}

    if not measured:
        return False, "no_coverage_data", {}

    file_deps: Dict[str, Tuple[list, str]] = {}
    for fname in measured:
        # Only track files inside the source root we care about.
        try:
            rel = os.path.relpath(fname, source_root)
        except ValueError:
            continue
        if rel.startswith(".."):
            continue

        lines: Set[int] = set(cov_data.lines(fname) or [])
        if not lines:
            continue

        fp, sha = coverage_fingerprint(fname, lines)
        if sha is None:
            continue
        file_deps[fname] = (fp, sha)

    return True, None, file_deps


# --------------------------------------------------------------------------- #
# Full survey over all benchmarks                                               #
# --------------------------------------------------------------------------- #

def run_survey(
    benchmark_dir: str,
    source_root: str,
    db: LightspeedDB,
    all_bids: list,  # List[BenchmarkId]
    verbose: bool = False,
) -> Dict[BenchmarkId, Optional[str]]:
    """
    Run the coverage survey for every benchmark in *all_bids*.
    Writes results into *db* and returns a mapping of
    BenchmarkId -> failure_reason (None = success).
    """
    failures: Dict[BenchmarkId, Optional[str]] = {}

    for bid in all_bids:
        if verbose:
            print(f"  surveying {bid}...", end=" ", flush=True)

        success, reason, file_deps = survey_one(benchmark_dir, bid, source_root)

        if success:
            db.store_deps_batch(bid, file_deps)
            failures[bid] = None
            if verbose:
                print(f"ok ({len(file_deps)} files)")
        else:
            db.set_always_affected(bid, reason or "unknown")
            failures[bid] = reason
            if verbose:
                print(f"FAILED ({reason}) - always_affected")

    return failures
