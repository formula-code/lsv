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
import contextlib
import glob
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import coverage
from asv_runner._aux import update_sys_path
from asv_runner.discovery import get_benchmark_from_name

from .deps_db import COMPILED_SUFFIXES, BenchmarkId, LightspeedDB
from .fingerprint import coverage_fingerprint, file_method_checksums


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
# C extension dependency tracking                                               #
# --------------------------------------------------------------------------- #

_PROJECT_FILES = ("pyproject.toml", "setup.py", "setup.cfg")
_BUILD_FILES = ("setup.py", "meson.build")
_SKIP_DIRS = {"build", "dist", "target", "node_modules", "__pycache__", "site-packages"}
_PY_LITERAL = re.compile(r"""["']([^"'\s]+\.py)["']""")

BuildScan = Tuple[List[str], Dict[str, List[str]]]


def _build_root(source_root: str) -> str:
    """The nearest folder at or above *source_root* with a project file, not above the repository root."""
    d = os.path.abspath(source_root)
    while True:
        if any(os.path.isfile(os.path.join(d, f)) for f in _PROJECT_FILES):
            return d
        parent = os.path.dirname(d)
        if os.path.exists(os.path.join(d, ".git")) or parent == d:
            return source_root
        d = parent


def _cythonized_py(build_file: str, root: str) -> Set[str]:
    """.py files a build file compiles: named next to a .pyx of the same stem, or inside a cythonize(...) call."""
    try:
        text = Path(build_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    names = {
        m for m in _PY_LITERAL.findall(text)
        if re.search(r"(?<![\w/.-])" + re.escape(m[:-3]) + r"\.pyx", text)
    }
    for m in re.finditer(r"cythonize\s*\(", text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        names |= set(_PY_LITERAL.findall(text[m.end():i]))
    found: Set[str] = set()
    for name in names:
        for base in {os.path.dirname(build_file), root}:
            found.update(p for p in glob.glob(os.path.join(base, name)) if os.path.isfile(p))
    return found


def scan_build(source_root: str, benchmark_dir: Optional[str] = None) -> BuildScan:
    """(compiled sources, other .py files by stem) under the build root of *source_root*."""
    root = _build_root(source_root)
    skip = os.path.abspath(benchmark_dir) if benchmark_dir else None
    compiled: Set[str] = set()
    py: Dict[str, List[str]] = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [
            x for x in dirs
            if not x.startswith(".") and x not in _SKIP_DIRS and os.path.join(d, x) != skip
            and not any(os.path.exists(os.path.join(d, x, m)) for m in ("conda-meta", "pyvenv.cfg"))
        ]
        for f in files:
            p = os.path.join(d, f)
            if f.endswith(COMPILED_SUFFIXES):
                compiled.add(p)
            elif f.endswith(".py"):
                py.setdefault(f[:-3], []).append(p)
            if f in _BUILD_FILES:
                compiled |= _cythonized_py(p, root)
    for paths in py.values():
        paths[:] = [p for p in paths if p not in compiled]
    return sorted(compiled), py


_checksum_cache: Dict[Tuple[str, int, int], Tuple[list, Optional[str]]] = {}


def _whole_file(path: str) -> Tuple[list, Optional[str]]:
    try:
        st = os.stat(path)
    except OSError:
        return [], None
    key = (path, st.st_mtime_ns, st.st_size)
    if key not in _checksum_cache:
        _checksum_cache[key] = file_method_checksums(path)
    return _checksum_cache[key]


def _add_extension_deps(
    file_deps: Dict[str, Tuple[list, str]],
    source_root: str,
    scan: Optional[BuildScan] = None,
) -> None:
    """
    When any extension module (``.so``/``.pyd``) is loaded, add every compiled
    source of the project's build root to *file_deps* with whole-file
    fingerprints, plus each ``.py`` file that is loaded as an extension module
    (compiled by Cython or mypyc). Coverage.py cannot trace C code, so this is
    how C/Cython changes select benchmarks.
    """
    ext_modules = [
        getattr(mod, "__name__", "") for mod in list(sys.modules.values())
        if mod is not None and (getattr(mod, "__file__", None) or "").endswith((".so", ".pyd"))
    ]
    ext_modules = [m for m in ext_modules if m]
    if not ext_modules:
        return
    compiled, py = scan if scan is not None else scan_build(source_root)
    paths = list(compiled)
    for name in ext_modules:
        tail = os.sep + name.replace(".", os.sep) + ".py"
        paths += [p for p in py.get(name.rsplit(".", 1)[-1], []) if p.endswith(tail)]
    for fpath in paths:
        fp, sha = _whole_file(fpath)
        if sha is not None:
            file_deps[fpath] = (fp, sha)


# --------------------------------------------------------------------------- #
# Child process coverage                                                        #
# --------------------------------------------------------------------------- #

# Shadows any sitecustomize on the child's path, so it runs that one afterwards.
_CHILD_SITECUSTOMIZE = """\
import os, sys
try:
    import coverage
    coverage.process_startup()
except Exception:
    pass
from importlib.machinery import PathFinder
from importlib.util import module_from_spec
_here = os.path.dirname(os.path.realpath(__file__))
_spec = PathFinder.find_spec(
    "sitecustomize", [p for p in sys.path if os.path.realpath(p or ".") != _here]
)
if _spec is not None:
    _mod = module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
"""


@contextlib.contextmanager
def _child_coverage(source_root: str):
    """Child Python processes started in the block write coverage data files to the yielded dir."""
    child_dir = tempfile.mkdtemp(prefix="lsv-cov-")
    rcfile = os.path.join(child_dir, "coveragerc")
    with open(rcfile, "w") as f:
        f.write(
            "[run]\n"
            "parallel = True\n"
            f"data_file = {os.path.join(child_dir, '.coverage')}\n"
            f"source = {source_root}\n"
            "omit = */site-packages/*, */dist-packages/*\n"
            "disable_warnings = no-data-collected, module-not-measured\n"
        )
    with open(os.path.join(child_dir, "sitecustomize.py"), "w") as f:
        f.write(_CHILD_SITECUSTOMIZE)

    saved = {k: os.environ.get(k) for k in ("COVERAGE_PROCESS_START", "PYTHONPATH")}
    os.environ["COVERAGE_PROCESS_START"] = rcfile
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [child_dir] + ([saved["PYTHONPATH"]] if saved["PYTHONPATH"] else [])
    )
    try:
        yield child_dir
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(child_dir, ignore_errors=True)


def _child_lines(child_dir: str) -> Dict[str, Set[int]]:
    """Executed lines per file, read from the child data files in *child_dir*."""
    lines: Dict[str, Set[int]] = {}
    for path in glob.glob(os.path.join(child_dir, ".coverage.*")):
        data = coverage.CoverageData(basename=path)
        data.read()
        for fname in data.measured_files():
            lines.setdefault(fname, set()).update(data.lines(fname) or [])
    return lines


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
    scan: Optional[BuildScan] = None,
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
    # sys.monitoring (the default core from Python 3.14) does not support switch_context.
    with contextlib.suppress(Exception):
        cov.set_option("run:core", "ctrace")
    # Child processes may be the only place the benchmark runs project code.
    cov.set_option(
        "run:disable_warnings", [*cov.get_option("run:disable_warnings"), "no-data-collected"]
    )

    tracer_before = sys.gettrace()
    profiler_before = sys.getprofile()

    # Coverage runs during setup so that threads started there are traced; only the "body" context is kept.
    skip = False
    cov.start()
    cov.switch_context("setup")
    try:
        skip = bench.do_setup()
    except BaseException as exc:
        cov.stop()
        return False, f"setup_error: {exc}", {}

    if skip:
        cov.stop()
        return False, "benchmark_skipped", {}

    try:
        with _child_coverage(source_root) as child_dir:
            cov.switch_context("body")
            try:
                bench.func(*bench._current_params)
            finally:
                cov.stop()
            child_lines = _child_lines(child_dir)
    except BaseException as exc:
        cov.stop()
        # Teardown runs once, in the finally below; a failing teardown must not stop the survey of the other benchmarks.
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
        cov_data.set_query_contexts(["^body$"])
        measured = set(cov_data.measured_files()) | set(child_lines)
    except Exception as exc:
        return False, f"coverage_read_error: {exc}", {}

    file_deps: Dict[str, Tuple[list, str]] = {}
    # A covered file without a fingerprint makes the benchmark always_affected, so its changes are not missed.
    uncomputable: Optional[str] = None
    if measured:
        for fname in measured:
            # Only track files inside the source root we care about.
            try:
                rel = os.path.relpath(fname, source_root)
            except ValueError:
                continue
            if rel.startswith(".."):
                continue

            lines: Set[int] = set(cov_data.lines(fname) or []) | child_lines.get(fname, set())
            if not lines:
                # With source=, coverage also lists files that never ran; they are not deps of this benchmark.
                continue

            fp, sha = coverage_fingerprint(fname, lines)
            if sha is None:
                if uncomputable is None:
                    uncomputable = fname
                continue
            file_deps[fname] = (fp, sha)

    # Mixed Python/C projects need both the coverage deps and the C extension deps.
    _add_extension_deps(file_deps, source_root, scan)

    if uncomputable is not None:
        return False, f"uncomputable fingerprint: {uncomputable}", {}

    if not file_deps:
        return False, "no_source_root_coverage", {}

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
    scan = scan_build(source_root, benchmark_dir)

    for bid in all_bids:
        if verbose:
            print(f"  surveying {bid}...", end=" ", flush=True)

        success, reason, file_deps = survey_one(benchmark_dir, bid, source_root, scan)

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
