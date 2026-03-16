# Licensed under a 3-clause BSD style license - see LICENSE.rst

import argparse
import os
import subprocess

from .. import util
from ..benchmarks import Benchmarks
from ..console import log
from ..environment import get_environments
from ..runner import run_benchmarks
from ..contrib.lightspeed.deps_db import BenchmarkId, LightspeedDB
from ..contrib.lightspeed.fingerprint import changed_files_with_fingerprints
from . import Command, common_args

_DEPS_DB_FILENAME = ".lightspeed_deps.db"


def _get_git_changed_files():
    try:
        out = subprocess.check_output(
            ["git", "diff", "HEAD", "--name-only"], stderr=subprocess.DEVNULL,
        ).decode().strip()
        return [os.path.abspath(p) for p in out.splitlines()] if out else []
    except subprocess.CalledProcessError as exc:
        raise util.UserError(f"'git diff HEAD' failed: {exc}")


def _extract_results(asv_results, benchmarks):
    """Pull timing stats from a Results object into dict[BenchmarkId, dict]."""
    out = {}
    for name, benchmark in benchmarks.items():
        result_vals = asv_results._results.get(name)
        stats_list = asv_results._stats.get(name)
        if result_vals is None or stats_list is None:
            continue
        if benchmark.get('params'):
            for param_idx, (val, stat) in enumerate(zip(result_vals, stats_list)):
                if val is not None and stat is not None:
                    out[BenchmarkId(name, param_idx)] = {'median': val, **stat}
        else:
            val = result_vals[0] if result_vals else None
            stat = stats_list[0] if stats_list else None
            if val is not None and stat is not None:
                out[BenchmarkId(name)] = {'median': val, **stat}
    return out


def _format_time(s):
    if s is None:
        return "n/a"
    if s >= 1.0:
        return f"{s:.3f}s"
    if s >= 1e-3:
        return f"{s * 1e3:.3f}ms"
    if s >= 1e-6:
        return f"{s * 1e6:.3f}us"
    return f"{s * 1e9:.3f}ns"


def _print_delta_table(deltas):
    if not deltas:
        return
    col_w = max(len(str(bid)) for bid in deltas) + 2
    header = f"{'benchmark':<{col_w}} {'baseline':>12} {'current':>12} {'delta':>10}"
    print(header)
    print("-" * len(header))
    for bid, info in sorted(deltas.items(), key=lambda x: str(x[0])):
        pct = info['delta'] * 100
        print(
            f"{str(bid):<{col_w}} "
            f"{_format_time(info['baseline_median']):>12} "
            f"{_format_time(info['current_median']):>12} "
            f"{'+' if pct >= 0 else ''}{pct:>8.1f}%"
        )


class MeasureImpacted(Command):
    @classmethod
    def setup_arguments(cls, subparsers):
        parser = subparsers.add_parser(
            "measure_impacted",
            help="Run benchmarks affected by changed files and report deltas",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=(
                "Query the dependency database (built by 'asv initialize_diffcheck')\n"
                "to find benchmarks whose execution paths touch the changed files,\n"
                "run only those benchmarks, and compare against the stored baseline.\n\n"
                "examples:\n"
                "  asv measure_impacted --changed-files src/foo.py src/bar.py\n"
                "  asv measure_impacted --from-git-diff\n"
                "  asv measure_impacted --from-git-diff --factor 1.05"
            ),
        )
        common_args.add_environment(parser, default_same=True)

        source_group = parser.add_mutually_exclusive_group(required=True)
        source_group.add_argument(
            "--changed-files", nargs="+", metavar="FILE",
            help="Explicit list of changed source files.",
        )
        source_group.add_argument(
            "--from-git-diff", action="store_true",
            help="Auto-detect changed files via 'git diff HEAD --name-only'.",
        )
        parser.add_argument(
            "--step-id", default=None, metavar="ID",
            help="Optional step label passed to _on_step_results() hook.",
        )
        parser.add_argument(
            "--factor", type=float, default=1.1,
            help="Exit non-zero if any benchmark regresses by more than this factor (default: 1.1).",
        )
        common_args.add_bench(parser)
        common_args.add_launch_method(parser)
        parser.set_defaults(func=cls.run_from_args)
        return parser

    @classmethod
    def run_from_conf_args(cls, conf, args):
        return cls.run(
            conf=conf,
            changed_files=args.changed_files,
            from_git_diff=args.from_git_diff,
            step_id=args.step_id,
            factor=args.factor,
            env_spec=args.env_spec,
            bench=args.bench,
            launch_method=getattr(args, 'launch_method', None),
        )

    @classmethod
    def run(
        cls, conf, changed_files=None, from_git_diff=False,
        step_id=None, factor=1.1, env_spec=None, bench=None, launch_method=None,
    ):
        env_spec = env_spec or ["existing:same"]
        environments = list(get_environments(conf, env_spec))
        if not environments:
            raise util.UserError("No environments available")
        conf.dvcs = "none"

        benchmarks = Benchmarks.load(conf, regex=bench)

        db_path = os.path.join(conf.results_dir, _DEPS_DB_FILENAME)
        if not os.path.exists(db_path):
            raise util.UserError(
                f"Dependency database not found: {db_path}\n"
                "Run 'asv initialize_diffcheck --source-root <path>' first."
            )
        db = LightspeedDB(db_path)

        baseline = {str(bid): b for bid in db.get_all_benchmark_ids() if (b := db.get_baseline(bid))}

        if from_git_diff:
            changed_files = _get_git_changed_files()
        else:
            changed_files = [os.path.abspath(p) for p in (changed_files or [])]

        if not changed_files:
            log.info("No changed files — nothing to do.")
            return 0

        changes = changed_files_with_fingerprints(changed_files, db.get_stored_fshas())
        affected_bids = db.get_affected_benchmark_ids(changes)
        if not affected_bids:
            log.info("No benchmarks are affected by the changed files.")
            return 0

        affected_names = {bid.name for bid in affected_bids}
        filtered = benchmarks.filter_out(set(benchmarks.keys()) - affected_names)

        log.info(f"Running {len(filtered)} affected benchmark(s)...")
        env = environments[0]
        env.create()
        lm = launch_method or getattr(conf, 'launch_method', None) or 'auto'
        results = run_benchmarks(filtered, env, launch_method=lm)

        step_results = _extract_results(results, filtered)
        deltas = {}
        for bid, timing in step_results.items():
            base = baseline.get(str(bid))
            if base is None:
                log.warning(f"No baseline for {bid} — skipping delta")
                continue
            delta = (timing['median'] - base['median']) / base['median']
            deltas[bid] = {
                'baseline_median': base['median'],
                'current_median': timing['median'],
                'delta': delta,
                **timing,
            }

        cls._on_step_results(results, deltas, step_id)
        _print_delta_table(deltas)

        regressions = [bid for bid, d in deltas.items() if d['delta'] > (factor - 1)]
        if regressions:
            log.warning(f"{len(regressions)} benchmark(s) regressed by more than factor {factor}:")
            with log.indent():
                for bid in regressions:
                    log.warning(str(bid))
            return 1
        return 0

    # @classmethod
    # def _on_step_results(cls, results, deltas, step_id):
    #     """No-op hook. Override in a fork subclass to persist step results."""
