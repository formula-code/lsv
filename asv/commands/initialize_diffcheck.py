# Licensed under a 3-clause BSD style license - see LICENSE.rst

import argparse

from ..console import log
from ..contrib.lightspeed.session import LightspeedSession
from . import Command, common_args


def add_lightspeed_arguments(parser):
    """The options that initialize_diffcheck and measure_impacted share."""
    common_args.add_environment(parser, default_same=True)
    common_args.add_bench(parser)
    common_args.add_launch_method(parser)
    parser.add_argument(
        "--rounds", type=int, default=None, metavar="N",
        help="Timing rounds per benchmark (default: the benchmark's own setting, typically 2).",
    )
    parser.add_argument(
        "--repeat", type=int, default=None, metavar="N",
        help="Samples collected per round (default: auto, 1–10).",
    )
    parser.add_argument(
        "--warmup-time", type=float, default=None, metavar="SECS",
        help="Seconds spent warming up before timing (default: auto).",
    )


def lightspeed_kwargs(args):
    return dict(
        rounds=args.rounds,
        repeat=args.repeat,
        warmup_time=args.warmup_time,
        launch_method=getattr(args, "launch_method", None),
    )


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
        add_lightspeed_arguments(parser)
        parser.set_defaults(func=cls.run_from_args)
        return parser

    @classmethod
    def run_from_conf_args(cls, conf, args):
        return cls.run(
            conf=conf,
            source_root=args.source_root,
            force=args.force,
            **lightspeed_kwargs(args),
        )

    @classmethod
    def run(cls, conf, source_root, force=False, rounds=None, repeat=None,
            warmup_time=None, launch_method=None):
        if launch_method:
            conf.launch_method = launch_method

        session = LightspeedSession._from_conf(conf)
        result = session.initialize_diffcheck(
            source_root, force=force,
            rounds=rounds, repeat=repeat, warmup_time=warmup_time,
        )

        log.info(
            f"Done. {len(result.benchmarks_discovered)} benchmark(s) discovered, "
            f"{len(result.benchmarks_impactable)} impactable, "
            f"{result.source_files_covered} source file(s) covered."
        )
        log.info(f"Dependency DB: {result.deps_db_path}")
        if result.timing.phases:
            log.info(
                f"Timing: survey={result.timing.phases['coverage']:.1f}s  "
                f"benchmarks={result.timing.phases['benchmarking']:.1f}s"
            )
        return 0
