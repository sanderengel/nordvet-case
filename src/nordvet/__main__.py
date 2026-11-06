"""Entry point: python -m nordvet <command>."""

from __future__ import annotations

import argparse
import sys

from nordvet.smoke import run_smoke

EXIT_NOT_IMPLEMENTED = 3


def run_pipeline() -> int:
    # Stages are added in later phases: ingest (3), model and metrics (4), report (5).
    print("The pipeline has no stages yet. Run `python -m nordvet smoke` to check the setup.")
    return EXIT_NOT_IMPLEMENTED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet", description="Nordvet no-show pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("smoke", help="check config, API reachability and the database")
    sub.add_parser("run", help="run the full pipeline (not implemented yet)")
    args = parser.parse_args(argv)
    return run_smoke() if args.command == "smoke" else run_pipeline()


if __name__ == "__main__":
    sys.exit(main())
