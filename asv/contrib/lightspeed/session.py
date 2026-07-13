# Licensed under a 3-clause BSD style license - see LICENSE.rst

"""
Python API for ASV Lightspeed — selective benchmark re-running for RL pipelines.

    from asv.contrib.lightspeed import LightspeedSession

    session = LightspeedSession(
        "/workspace/repo/benchmarks/asv.conf.json",
        overrides={"results_dir": "/output/results"},
        machine="ci",
    )
    init   = session.initialize_diffcheck(source_root="/workspace/repo/shapely")
    result = session.measure_impacted(changed_files=["src/foo.py"])
    for name, delta in result.benchmarks.items():
        print(f"{name}: {delta.baseline_str} -> {delta.current_str} ({delta.delta_pct:+.1f}%)")
"""

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
from .deps_db import BenchmarkId, LightspeedDB, _parse_bid
from .fingerprint import changed_files_with_fingerprints
from .survey import run_survey

_DEPS_DB_FILENAME = ".lightspeed_deps.db"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ASVError(Exception):
    """Base exception for all Lightspeed API errors."""


class ConfigError(ASVError):
    """Invalid or unreadable config file."""


class BenchmarkError(ASVError):
    """A benchmark failed to run."""
    def __init__(self, message, *, benchmark_name=None, stderr=None):
        super().__init__(message)
        self.benchmark_name = benchmark_name
        self.stderr = stderr


class NoBenchmarksError(ASVError):
    """No benchmarks were selected or found."""


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    """
    Run ``measure()`` with the tracked working tree reset to ``base_commit``,
    preserving untracked files (e.g. staged ``benchmark_*.py``), then restore.

    Two cases:

    * HEAD already *is* ``base_commit`` (the normal in-container state after the
      entrypoint's ``git reset --hard <base>``, with the change under test applied
      as uncommitted tracked modifications).  A plain ``git stash push`` — *not*
      ``--include-untracked`` — reverts tracked files to base while leaving
      untracked benchmark files in place; ``git stash pop`` restores the patch.
    * HEAD differs from ``base_commit`` (defensive fallback): stash the patched
      source path, ``git checkout <base> -- <path>`` to reset just that path to
      base, measure, then restore the path to HEAD and pop the stash.
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


# ---------------------------------------------------------------------------
# LightspeedSession
# ---------------------------------------------------------------------------

class LightspeedSession:
    """
    Main entry point for the Lightspeed Python API.

    Parameters
    ----------
    config_path : str or Path
        Path to ``asv.conf.json``.  Resolved to absolute; relative paths in
        the config are resolved against this file's parent directory.
    overrides : dict, optional
        Config field overrides applied in-memory.  Any key valid in the JSON
        config is valid here (e.g. ``results_dir``, ``repo``, ``branches``).
    machine : str, optional
        Machine name.  Defaults to ``socket.gethostname()``.
        ``{results_dir}/{machine}/machine.json`` is created automatically.
    python : str, optional
        Python spec.  ``"same"`` (default) uses the current interpreter.
    """

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

        # Resolve relative paths against the config file's parent directory so
        # the session works regardless of the caller's cwd.
        config_dir = config_path.parent
        for attr in ("benchmark_dir", "results_dir", "html_dir", "env_dir"):
            val = getattr(conf, attr, None)
            if val and not Path(val).is_absolute():
                setattr(conf, attr, str(config_dir / val))

        self._setup(conf, config_path, machine, python)

    @classmethod
    def _from_conf(cls, conf, config_path=None, *, machine=None, python="same"):
        """
        Create a session from an already-loaded Config object.

        Used by CLI commands, which receive a pre-loaded conf from ASV's
        ``Command.run_from_args`` machinery.  Paths in *conf* are left as-is.
        """
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

    # --- Properties --------------------------------------------------------

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

    # --- Internal ----------------------------------------------------------

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

    # --- Public API --------------------------------------------------------

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
        """
        Survey benchmark dependencies and record baseline timing.

        Pass 1 — coverage survey: record which source files each benchmark
        touches, storing method-level fingerprints in ``.lightspeed_deps.db``.

        Pass 2 — baseline timing: measure current performance via ASV's full
        timing protocol and store results in the same SQLite database.

        Parameters
        ----------
        source_root : str or Path
            Root of the source package to analyse.  Only files within this
            directory are recorded in the dependency table.
        force : bool
            Re-run both passes even if a baseline already exists.
        rounds : int, optional
            Number of timing rounds per benchmark.  Defaults to the benchmark's
            own setting (typically 2 via the ``processes`` backward-compat
            attribute).  More rounds → more accurate baseline at the cost of
            wall-clock time.
        repeat : int, optional
            Samples collected per round.  ``None`` means auto (ASV picks 1–10,
            halved when ``rounds > 1``).
        warmup_time : float, optional
            Seconds spent warming up before timing begins.  ``None`` means auto
            (≈1 s for multi-round, ≈5 s for single-round).
        base_commit : str, optional
            When set and a baseline must be measured (cache miss or ``force``),
            stash the working tree, reset the tracked source to this commit,
            measure, then restore — so the recorded baseline reflects the base
            commit rather than whatever is currently checked out (e.g. patched
            code under test).  Untracked files (such as staged ``benchmark_*.py``)
            are preserved.  Requires ``launch_method`` 'spawn' (the default);
            'forkserver' is rejected because it pre-imports the suite once and
            would not reflect the checkout.  ``None`` disables all git side
            effects and preserves the previous behavior.
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
    ) -> MeasureResult:
        """
        Selectively re-run benchmarks affected by code changes.

        Exactly one of ``from_git_diff=True`` or ``changed_files=[...]``
        must be provided.

        Parameters
        ----------
        from_git_diff : bool
            Detect changed files via ``git diff HEAD --name-only``.
        changed_files : list of str or Path, optional
            Explicit list of changed file paths.  Takes precedence over
            ``from_git_diff`` if both are supplied.
        rounds : int, optional
            Number of timing rounds.  For per-step RL measurements, ``1`` is
            usually sufficient since deltas are relative to the baseline.
        repeat : int, optional
            Samples per round.  ``None`` means auto.
        warmup_time : float, optional
            Warmup seconds.  ``None`` means auto.
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
        asv_results = run_benchmarks(filtered, env, extra_params=extra_params, launch_method=lm)

        deltas = _extract_deltas(asv_results, filtered, baseline)

        return MeasureResult(
            benchmarks=deltas,
            selected_count=len(filtered),
            total_count=total_count,
            skipped_count=total_count - len(filtered),
            timing=TimingInfo(total_s=time.perf_counter() - t0),
        )

    def get_results(self) -> Dict[str, Optional[float]]:
        """
        Return stored baseline timing from the SQLite database.

        Returns
        -------
        dict[str, float | None]
            Map of benchmark ID string to baseline median time in seconds.
        """
        if not self.deps_db_path.exists():
            return {}
        db = LightspeedDB(str(self.deps_db_path))
        rows = db._con.execute(
            "SELECT benchmark_id, median FROM baseline"
        ).fetchall()
        return {r["benchmark_id"]: r["median"] for r in rows}

    def export_baselines(self) -> Dict[str, Dict[str, float]]:
        """
        Dump every stored baseline as a serializable mapping.

        Returns
        -------
        dict[str, dict]
            ``{benchmark_id_str: {"median", "ci_99_a", "ci_99_b", "q_25", "q_75",
            "repeat", "number"}}``.  Empty dict if the deps DB does not exist or
            no baselines have been recorded yet.

        Notes
        -----
        Pair with :meth:`load_baselines` to ship measured baselines between
        runs (e.g. cache the results of ``initialize_diffcheck`` keyed by
        machine fingerprint, then reload them on a fresh sandbox to skip the
        timing pass).
        """
        if not self.deps_db_path.exists():
            return {}
        db = LightspeedDB(str(self.deps_db_path))
        out: Dict[str, Dict[str, float]] = {}
        for bid in db.get_all_benchmark_ids():
            row = db.get_baseline(bid)
            if row is None:
                continue
            out[str(bid)] = {
                "median":  row["median"],
                "ci_99_a": row["ci_99_a"],
                "ci_99_b": row["ci_99_b"],
                "q_25":    row["q_25"],
                "q_75":    row["q_75"],
                "repeat":  row["repeat"],
                "number":  row["number"],
            }
        return out

    def load_baselines(self, payload: Dict[str, Dict[str, float]]) -> int:
        """
        Bulk-insert (or replace) baseline timings from a serialized payload.

        Parameters
        ----------
        payload : dict
            Output of :meth:`export_baselines` from a prior run on equivalent
            hardware.  Each value must contain the same keys exported there.

        Returns
        -------
        int
            Number of baseline rows written.

        Raises
        ------
        FileNotFoundError
            If ``deps_db_path`` does not yet exist; stage a cached deps DB
            (or run :meth:`initialize_diffcheck` once) before calling this.

        Notes
        -----
        After this returns, ``initialize_diffcheck(force=False)`` will
        short-circuit when every benchmark id has a baseline row.
        """
        if not self.deps_db_path.exists():
            raise FileNotFoundError(
                f"deps DB missing at {self.deps_db_path}; "
                "stage a cached copy or run initialize_diffcheck first"
            )
        db = LightspeedDB(str(self.deps_db_path))
        n = 0
        for bid_str, stats in payload.items():
            db.store_baseline(_parse_bid(bid_str), stats["median"], stats)
            n += 1
        return n
