# Licensed under a 3-clause BSD style license - see LICENSE.rst

import argparse

from ..console import log
from ..contrib.lightspeed.session import LightspeedSession, MeasureResult
from . import Command, common_args


def _print_delta_table(result: MeasureResult):
    if not result.benchmarks:
        return
    col_w = max(len(name) for name in result.benchmarks) + 2
    header = f"{'benchmark':<{col_w}} {'baseline':>12} {'current':>12} {'delta':>10}"
    print(header)
    print("-" * len(header))
    for name, d in sorted(result.benchmarks.items()):
        pct = d.delta_pct
        pct_str = f"{'+' if pct >= 0 else ''}{pct:.1f}%" if pct is not None else "n/a"
        print(
            f"{name:<{col_w}} "
            f"{d.baseline_str:>12} "
            f"{d.current_str:>12} "
            f"{pct_str:>10}"
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
                "  asv measure_impacted --from-git-diff"
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
        common_args.add_bench(parser)
        common_args.add_launch_method(parser)
        parser.add_argument(
            "--rounds", type=int, default=None, metavar="N",
            help=(
                "Number of timing rounds per benchmark. For per-step RL "
                "measurements 1 is usually sufficient. Defaults to the "
                "benchmark's own setting (typically 2)."
            ),
        )
        parser.add_argument(
            "--repeat", type=int, default=None, metavar="N",
            help="Samples collected per round (default: auto, 1–10).",
        )
        parser.add_argument(
            "--warmup-time", type=float, default=None, metavar="SECS",
            help="Seconds spent warming up before timing (default: auto).",
        )
        parser.set_defaults(func=cls.run_from_args)
        return parser

    @classmethod
    def run_from_conf_args(cls, conf, args):
        return cls.run(
            conf=conf,
            changed_files=args.changed_files,
            from_git_diff=args.from_git_diff,
            step_id=args.step_id,
            rounds=args.rounds,
            repeat=args.repeat,
            warmup_time=args.warmup_time,
            launch_method=getattr(args, "launch_method", None),
        )

    @classmethod
    def run(
        cls, conf, changed_files=None, from_git_diff=False,
        step_id=None, rounds=None, repeat=None, warmup_time=None,
        launch_method=None,
    ):
        if launch_method:
            conf.launch_method = launch_method

        session = LightspeedSession._from_conf(conf)
        result = session.measure_impacted(
            from_git_diff=from_git_diff,
            changed_files=changed_files,
            rounds=rounds,
            repeat=repeat,
            warmup_time=warmup_time,
        )

        if not result.benchmarks:
            log.info(
                f"No benchmarks affected "
                f"({result.total_count} total, 0 selected)."
            )
            return 0

        log.info(
            f"Ran {result.selected_count}/{result.total_count} benchmark(s) "
            f"in {result.timing.total_s:.1f}s"
        )
        cls._on_step_results(result, step_id)
        _print_delta_table(result)
        return 0

    @classmethod
    def _on_step_results(cls, result: MeasureResult, step_id):
        """No-op hook. A subclass can override it to store step results."""
        pass
