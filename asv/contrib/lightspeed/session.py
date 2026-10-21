# Licensed under a 3-clause BSD style license - see LICENSE.rst

"""Python API for Lightspeed: re-run only the benchmarks that the changed files affect; see lightspeed_docs.md."""

import itertools
import json
import os
import platform
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ...benchmarks import Benchmarks
from ...config import Config
from ...environment import get_environments
from ...runner import run_benchmarks
from ... import util
from .deps_db import BenchmarkId, LightspeedDB
from .fingerprint import changed_files_with_fingerprints
from .project_imports import import_packages, outside_root, probe_imports
from .survey import run_survey

_DEPS_DB_FILENAME = ".lightspeed_deps.db"


class ASVError(Exception):
    """Base exception for all Lightspeed API errors."""


class ConfigError(ASVError):
    """Invalid or unreadable config file."""


class NoBenchmarksError(ASVError):
    """No benchmarks were selected or found."""


class ProjectShadowed(ASVError):
    """The benchmark processes import a project package from outside the repository."""
    def __init__(self, paths):
        super().__init__("benchmarks import the project from outside the repository: "
                         + ", ".join(f"{name} -> {path}" for name, path in paths.items()))
        self.paths = paths


def check_project_imports(env, benchmark_dir, launch_method, packages, repo_root):
    """Raise ProjectShadowed when a package resolves outside ``repo_root`` in a benchmark process."""
    found = probe_imports(env, benchmark_dir, launch_method, packages) if packages else {}
    bad = outside_root(found, repo_root)
    if bad:
        raise ProjectShadowed(bad)
    return found


@dataclass
class TimingInfo:
    total_s: float
    phases: Optional[Dict[str, float]] = None


@dataclass
class InitResult:
    benchmarks_discovered: List[str]   # All benchmark ID strings found
    benchmarks_impactable: List[str]   # Benchmarks with coverage-mapped deps
    source_files_covered: int          # Distinct source files in dep table
    deps_db_path: Path                 # Absolute path to .lightspeed_deps.db
    timing: TimingInfo


@dataclass
class BenchmarkDelta:
    name: str                          # Fully-qualified benchmark ID string
    baseline: Optional[float]          # Baseline median in seconds
    current: Optional[float]           # Current median in seconds
    delta_pct: Optional[float]         # % change (positive = slower)
    baseline_str: str                  # Human-readable, e.g. "1.230ms"
    current_str: str                   # Human-readable, e.g. "1.450ms"
    params: Optional[dict]             # Param dict for parameterised benchmarks


@dataclass
class MeasureResult:
    benchmarks: Dict[str, BenchmarkDelta]  # Keyed by benchmark ID string
    selected_count: int                     # Benchmarks actually re-run
    total_count: int                        # Total benchmarks in suite
    skipped_count: int                      # Benchmarks skipped (unaffected)
    timing: TimingInfo


def _fmt(s: Optional[float]) -> str:
    if s is None:
        return "n/a"
    if s >= 1.0:
        return f"{s:.3f}s"
    if s >= 1e-3:
        return f"{s * 1e3:.3f}ms"
    if s >= 1e-6:
        return f"{s * 1e6:.3f}us"
    return f"{s * 1e9:.3f}ns"


def _all_bids(benchmarks: Benchmarks) -> List[BenchmarkId]:
    bids = []
    for name, benchmark in benchmarks.items():
        params = benchmark.get("params")
        if params:
            for i in range(len(list(itertools.product(*params)))):
                bids.append(BenchmarkId(name, i))
        else:
            bids.append(BenchmarkId(name))
    return bids


def _store_baseline(asv_results, benchmarks: Benchmarks, db: LightspeedDB):
    for name, benchmark in benchmarks.items():
        result_vals = asv_results._results.get(name)
        stats_list = asv_results._stats.get(name)
        if result_vals is None or stats_list is None:
            continue
        if benchmark.get("params"):
            for idx, (val, stat) in enumerate(zip(result_vals, stats_list)):
                if val is not None and stat is not None:
                    db.store_baseline(BenchmarkId(name, idx), val, stat)
        else:
            val = result_vals[0] if result_vals else None
            stat = stats_list[0] if stats_list else None
            if val is not None and stat is not None:
                db.store_baseline(BenchmarkId(name), val, stat)


def _timing_params(rounds, repeat, warmup_time) -> dict:
    """Build the extra_params dict for run_benchmarks() from optional overrides."""
    p = {}
    if rounds is not None:
        p["rounds"] = rounds
    if repeat is not None:
        p["repeat"] = repeat
    if warmup_time is not None:
        p["warmup_time"] = warmup_time
    return p


def _git(repo_root, *args, check=True):
    """Run ``git <args>`` in ``repo_root`` and return stripped stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise ASVError(
            f"git {' '.join(args)} failed in {repo_root}: "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout.decode(errors="replace").strip()


def _git_toplevel(source_root):
    """Return the git worktree root containing ``source_root``, or None if not a repo."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=source_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace").strip() or None


def _measure_at_commit(repo_root, base_commit, source_root, measure):
    """Run ``measure()`` with the tracked files at ``base_commit``, keeping untracked files, then restore the tree.

    When HEAD is not ``base_commit``, only ``source_root`` is checked out at ``base_commit``.
    """
    head = _git(repo_root, "rev-parse", "HEAD")
    base = _git(repo_root, "rev-parse", base_commit)
    if head == base:
        out = _git(repo_root, "stash", "push", "-m", "lsv-baseline")
        stashed = "No local changes to save" not in out
        try:
            return measure()
        finally:
            if stashed:
                _git(repo_root, "stash", "pop")
    else:
        rel = os.path.relpath(source_root, repo_root)
        out = _git(repo_root, "stash", "push", "-m", "lsv-baseline", "--", rel)
        stashed = "No local changes to save" not in out
        try:
            _git(repo_root, "checkout", base, "--", rel)
            try:
                return measure()
            finally:
                _git(repo_root, "checkout", head, "--", rel)
        finally:
            if stashed:
                _git(repo_root, "stash", "pop")


def _extract_deltas(
    asv_results,
    benchmarks: Benchmarks,
    baseline: Dict[str, dict],
) -> Dict[str, BenchmarkDelta]:
    out: Dict[str, BenchmarkDelta] = {}
    for name, benchmark in benchmarks.items():
        result_vals = asv_results._results.get(name)
        if result_vals is None:
            continue
        params_spec = benchmark.get("params")
        if params_spec:
            combos = list(itertools.product(*params_spec))
            stats_list = asv_results._stats.get(name) or [None] * len(result_vals)
            for idx, (val, _stat) in enumerate(zip(result_vals, stats_list)):
                bid_str = str(BenchmarkId(name, idx))
                base = baseline.get(bid_str)
                delta_pct = None
                if val is not None and base is not None:
                    delta_pct = (val - base["median"]) / base["median"] * 100
                params_dict = (
                    {f"p{i}": v for i, v in enumerate(combos[idx])}
                    if idx < len(combos) else None
                )
                out[bid_str] = BenchmarkDelta(
                    name=bid_str,
                    baseline=base["median"] if base else None,
                    current=val,
                    delta_pct=delta_pct,
                    baseline_str=_fmt(base["median"] if base else None),
                    current_str=_fmt(val),
                    params=params_dict,
                )
        else:
            val = result_vals[0] if result_vals else None
            base = baseline.get(name)
            delta_pct = None
            if val is not None and base is not None:
                delta_pct = (val - base["median"]) / base["median"] * 100
            out[name] = BenchmarkDelta(
                name=name,
                baseline=base["median"] if base else None,
                current=val,
                delta_pct=delta_pct,
                baseline_str=_fmt(base["median"] if base else None),
                current_str=_fmt(val),
                params=None,
            )
    return out


class LightspeedSession:
    """An asv config with *overrides* applied; the parameters are in lightspeed_docs.md."""

    def __init__(
        self,
        config_path,
        *,
        overrides: Optional[dict] = None,
        machine: Optional[str] = None,
        python: str = "same",
    ):
        config_path = Path(config_path).resolve()
        try:
            conf = Config.load(str(config_path))
        except Exception as exc:
            raise ConfigError(f"Failed to load config {config_path}: {exc}") from exc

        if overrides:
            for k, v in overrides.items():
                setattr(conf, k, v)

        # Relative config paths are taken from the config file's folder, not the cwd.
        config_dir = config_path.parent
        for attr in ("benchmark_dir", "results_dir", "html_dir", "env_dir"):
            val = getattr(conf, attr, None)
            if val and not Path(val).is_absolute():
                setattr(conf, attr, str(config_dir / val))

        self._setup(conf, config_path, machine, python)

    @classmethod
    def _from_conf(cls, conf, config_path=None, *, machine=None, python="same"):
        """A session from a loaded Config, for the CLI commands; paths in *conf* are used as they are."""
        session = cls.__new__(cls)
        session._setup(conf, config_path, machine, python)
        return session

    def _setup(self, conf, config_path, machine, python):
        conf.dvcs = "none"
        self._conf = conf
        self._config_path = Path(config_path).resolve() if config_path else None
        self._machine = machine or socket.gethostname()
        self._python = python
        self._ensure_machine_dir()

    @property
    def config_path(self) -> Optional[Path]:
        return self._config_path

    @property
    def benchmark_dir(self) -> Path:
        return Path(self._conf.benchmark_dir)

    @property
    def results_dir(self) -> Path:
        return Path(self._conf.results_dir)

    @property
    def env_dir(self) -> Path:
        return Path(self._conf.env_dir)

    @property
    def repo(self) -> str:
        return self._conf.repo

    @property
    def machine(self) -> str:
        return self._machine

    @property
    def python(self) -> str:
        return self._python

    @property
    def deps_db_path(self) -> Path:
        return Path(self._conf.results_dir) / _DEPS_DB_FILENAME

    def _ensure_machine_dir(self):
        machine_dir = Path(self._conf.results_dir) / self._machine
        machine_dir.mkdir(parents=True, exist_ok=True)
        machine_json = machine_dir / "machine.json"
        if not machine_json.exists():
            machine_json.write_text(json.dumps({
                "machine": self._machine,
                "os": platform.platform(),
                "arch": platform.machine(),
                "cpu": platform.processor() or "unknown",
                "ram": "0",
                "version": 1,
            }, indent=2))

    def _get_env(self):
        envs = list(get_environments(self._conf, [f"existing:{self._python}"]))
        if not envs:
            raise ASVError(f"No environment available for python={self._python!r}")
        env = envs[0]
        env.create()
        return env

    def _load_benchmarks(self, regex=None) -> Benchmarks:
        try:
            return Benchmarks.load(self._conf, regex=regex)
        except util.UserError:
            from ...repo import NoRepository
            envs = list(get_environments(self._conf, [f"existing:{self._python}"]))
            b = Benchmarks.discover(self._conf, NoRepository(), envs, [None], regex=regex)
            b.save()
            return b

    def initialize_diffcheck(
        self,
        source_root,
        *,
        force: bool = False,
        rounds: Optional[int] = None,
        repeat: Optional[int] = None,
        warmup_time: Optional[float] = None,
        base_commit: Optional[str] = None,
    ) -> InitResult:
        """Survey the benchmark dependencies, then time every benchmark and store the baseline.

        With *base_commit*, both passes run on the tracked files of that commit; untracked files stay.
        """
        t0 = time.perf_counter()
        phases: Dict[str, float] = {}

        source_root = str(Path(source_root).resolve())
        if not os.path.isdir(source_root):
            raise ASVError(f"source_root is not a directory: {source_root}")

        benchmarks = self._load_benchmarks()
        if not benchmarks:
            raise NoBenchmarksError("No benchmarks found")

        bids = _all_bids(benchmarks)
        db = LightspeedDB(str(self.deps_db_path))

        if not force and all(db.has_baseline(bid) for bid in bids):
            impactable = [
                r[0] for r in db._con.execute(
                    "SELECT DISTINCT benchmark_id FROM benchmark_dep"
                ).fetchall()
            ]
            n_files = db._con.execute(
                "SELECT COUNT(DISTINCT filename) FROM benchmark_dep"
            ).fetchone()[0]
            return InitResult(
                benchmarks_discovered=[str(b) for b in bids],
                benchmarks_impactable=impactable,
                source_files_covered=n_files,
                deps_db_path=self.deps_db_path,
                timing=TimingInfo(total_s=time.perf_counter() - t0),
            )

        extra_params = _timing_params(rounds, repeat, warmup_time)
        env = self._get_env()
        lm = getattr(self._conf, "launch_method", None) or "auto"
        if base_commit and lm == "forkserver":
            raise ASVError(
                "base_commit requires launch_method 'spawn': 'forkserver' pre-imports "
                "the benchmark suite once, so a base-commit checkout would not be "
                "reflected in the measured baseline."
            )

        def _measure():
            t1 = time.perf_counter()
            run_survey(str(self.benchmark_dir), source_root, db, bids)
            phases["coverage"] = time.perf_counter() - t1
            t2 = time.perf_counter()
            results = run_benchmarks(
                benchmarks, env, extra_params=extra_params, launch_method=lm
            )
            phases["benchmarking"] = time.perf_counter() - t2
            return results

        repo_root = _git_toplevel(source_root) if base_commit else None
        if base_commit and repo_root:
            asv_results = _measure_at_commit(repo_root, base_commit, source_root, _measure)
        else:
            asv_results = _measure()

        _store_baseline(asv_results, benchmarks, db)

        impactable = [
            r[0] for r in db._con.execute(
                "SELECT DISTINCT benchmark_id FROM benchmark_dep"
            ).fetchall()
        ]
        n_files = db._con.execute(
            "SELECT COUNT(DISTINCT filename) FROM benchmark_dep"
        ).fetchone()[0]

        return InitResult(
            benchmarks_discovered=[str(b) for b in bids],
            benchmarks_impactable=impactable,
            source_files_covered=n_files,
            deps_db_path=self.deps_db_path,
            timing=TimingInfo(total_s=time.perf_counter() - t0, phases=phases),
        )

    def measure_impacted(
        self,
        *,
        from_git_diff: bool = False,
        changed_files=None,
        rounds: Optional[int] = None,
        repeat: Optional[int] = None,
        warmup_time: Optional[float] = None,
        packages: Optional[List[str]] = None,
    ) -> MeasureResult:
        """Time the benchmarks that *changed_files* (or ``git diff HEAD``) affect, against the baseline.

        Raises ProjectShadowed before timing when a project package imports from outside the repository.
        """
        if not from_git_diff and changed_files is None:
            raise ValueError("Provide either from_git_diff=True or changed_files=[...]")

        t0 = time.perf_counter()

        if not self.deps_db_path.exists():
            raise ASVError(
                f"Dependency database not found: {self.deps_db_path}\n"
                "Call initialize_diffcheck() first."
            )

        benchmarks = self._load_benchmarks()
        total_count = len(benchmarks)
        db = LightspeedDB(str(self.deps_db_path))

        baseline = {
            str(bid): b
            for bid in db.get_all_benchmark_ids()
            if (b := db.get_baseline(bid)) is not None
        }

        if changed_files is not None:
            paths = [os.path.abspath(p) for p in changed_files]
        else:
            try:
                repo_root = subprocess.check_output(
                    ["git", "rev-parse", "--show-toplevel"],
                    stderr=subprocess.DEVNULL,
                ).decode().strip()
                raw = subprocess.check_output(
                    ["git", "diff", "HEAD", "--name-only"],
                    stderr=subprocess.DEVNULL,
                ).decode().strip()
                paths = [os.path.abspath(os.path.join(repo_root, p)) for p in raw.splitlines()] if raw else []
            except subprocess.CalledProcessError as exc:
                raise ASVError(f"'git diff HEAD' failed: {exc}") from exc

        _empty = MeasureResult(
            benchmarks={},
            selected_count=0,
            total_count=total_count,
            skipped_count=total_count,
            timing=TimingInfo(total_s=time.perf_counter() - t0),
        )

        if not paths:
            return _empty

        changes = changed_files_with_fingerprints(paths, db.get_stored_fshas())
        affected_bids = db.get_affected_benchmark_ids(changes)
        if not affected_bids:
            return _empty

        affected_names = {bid.name for bid in affected_bids}
        filtered = benchmarks.filter_out(set(benchmarks.keys()) - affected_names)

        extra_params = _timing_params(rounds, repeat, warmup_time)
        env = self._get_env()
        lm = getattr(self._conf, "launch_method", None) or "auto"
        root = _git_toplevel(os.path.dirname(paths[0])) or self.repo
        check_project_imports(env, self.benchmark_dir, lm, packages or import_packages(paths, root), root)
        asv_results = run_benchmarks(filtered, env, extra_params=extra_params, launch_method=lm)

        deltas = _extract_deltas(asv_results, filtered, baseline)

        return MeasureResult(
            benchmarks=deltas,
            selected_count=len(filtered),
            total_count=total_count,
            skipped_count=total_count - len(filtered),
            timing=TimingInfo(total_s=time.perf_counter() - t0),
        )
