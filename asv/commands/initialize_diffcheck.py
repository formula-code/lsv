# Licensed under a 3-clause BSD style license - see LICENSE.rst

import argparse
import itertools
import os

from .. import util
from ..benchmarks import Benchmarks
from ..console import log
from ..environment import get_environments
from ..repo import NoRepository
from ..runner import run_benchmarks
from ..contrib.lightspeed.deps_db import BenchmarkId, LightspeedDB
from ..contrib.lightspeed.survey import run_survey
from . import Command, common_args

_DEPS_DB_FILENAME = ".lightspeed_deps.db"


def _get_all_bids(benchmarks):
    bids = []
    for name, benchmark in benchmarks.items():
        params = benchmark.get('params')
        if params:
            for i in range(len(list(itertools.product(*params)))):
                bids.append(BenchmarkId(name, i))
        else:
            bids.append(BenchmarkId(name))
    return bids


def _store_baseline_from_results(asv_results, benchmarks, db):
    """Write timing stats from a Results object into the baseline table."""
    for name, benchmark in benchmarks.items():
        result_vals = asv_results._results.get(name)
        stats_list = asv_results._stats.get(name)
        if result_vals is None or stats_list is None:
            continue
        if benchmark.get('params'):
            for param_idx, (val, stat) in enumerate(zip(result_vals, stats_list)):
                if val is not None and stat is not None:
                    db.store_baseline(BenchmarkId(name, param_idx), val, stat)
        else:
            val = result_vals[0] if result_vals else None
            stat = stats_list[0] if stats_list else None
            if val is not None and stat is not None:
                db.store_baseline(BenchmarkId(name), val, stat)


class InitializeDiffcheck(Command):
    @classmethod
    def setup_arguments(cls, subparsers):
        parser = subparsers.add_parser(
            "initialize_diffcheck",
            help="Survey benchmark dependencies and record baseline timing",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=(
                "Run two passes over all benchmarks:\n\n"
                "  1. Coverage survey — record which source files each benchmark\n"
                "     touches, storing method-level fingerprints in a SQLite DB.\n\n"
                "  2. Baseline timing — measure current performance using ASV's\n"
                "     full timing protocol.\n\n"
                "Results are stored in {results_dir}/.lightspeed_deps.db.\n"
            ),
        )
        common_args.add_environment(parser, default_same=True)
        parser.add_argument(
            "--source-root",
            required=True,
            metavar="PATH",
            help="Source package root to track. Only files within this directory are recorded.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-run both passes even if a baseline already exists.",
        )
        common_args.add_bench(parser)
        common_args.add_launch_method(parser)
        parser.set_defaults(func=cls.run_from_args)
        return parser

    @classmethod
    def run_from_conf_args(cls, conf, args):
        return cls.run(
            conf=conf,
            source_root=args.source_root,
            env_spec=args.env_spec,
            force=args.force,
            bench=args.bench,
            launch_method=getattr(args, 'launch_method', None),
        )

    @classmethod
    def run(cls, conf, source_root, env_spec=None, force=False, bench=None, launch_method=None):
        source_root = os.path.abspath(source_root)
        if not os.path.isdir(source_root):
            raise util.UserError(f"--source-root is not a directory: {source_root}")

        env_spec = env_spec or ["existing:same"]
        environments = list(get_environments(conf, env_spec))
        if not environments:
            raise util.UserError("No environments available")
        conf.dvcs = "none"

        try:
            benchmarks = Benchmarks.load(conf, regex=bench)
            log.info(f"Loaded {len(benchmarks)} benchmark(s) from benchmarks.json")
        except util.UserError:
            log.info("benchmarks.json not found — discovering benchmarks...")
            benchmarks = Benchmarks.discover(conf, NoRepository(), environments, [None], regex=bench)
            benchmarks.save()
            log.info(f"Discovered and saved {len(benchmarks)} benchmark(s)")

        if not benchmarks:
            log.error("No benchmarks found")
            return 1

        db_path = os.path.join(conf.results_dir, _DEPS_DB_FILENAME)
        db = LightspeedDB(db_path)
        all_bids = _get_all_bids(benchmarks)

        if not force and all(db.has_baseline(bid) for bid in all_bids):
            log.info("Baseline already exists for all benchmarks. Use --force to re-run.")
            return 0

        log.info("Pass 1: coverage survey")
        with log.indent():
            run_survey(conf.benchmark_dir, source_root, db, all_bids, verbose=True)

        env = environments[0]
        env.create()
        lm = launch_method or getattr(conf, 'launch_method', None) or 'auto'
        log.info("Pass 2: baseline timing")
        results = run_benchmarks(benchmarks, env, launch_method=lm)

        _store_baseline_from_results(results, benchmarks, db)

        stored = sum(1 for bid in all_bids if db.has_baseline(bid))
        log.info(f"Done. Baseline stored for {stored}/{len(all_bids)} benchmark(s) in {db_path}")
        return 0
