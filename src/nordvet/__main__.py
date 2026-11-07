"""Entry point: python -m nordvet <command>.

    python -m nordvet smoke               check the configuration, the API and the database
    python -m nordvet run [--full] [-v] [--open]
                                          the whole pipeline: load, build, export, report
"""

from __future__ import annotations

import argparse
import sys

from nordvet.pipeline import run_pipeline
from nordvet.smoke import run_smoke


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet", description="Nordvet no-show pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("smoke", help="check the configuration, the API and the database")
    run = sub.add_parser("run", help="the whole pipeline: load, build, export, report")
    run.add_argument("--full", action="store_true", help="load everything, ignoring earlier runs")
    run.add_argument("-v", "--verbose", action="store_true", help="debug logging, with tracebacks")
    run.add_argument("--open", action="store_true", help="open the report in the browser when it was written")
    args = parser.parse_args(argv)
    if args.command == "smoke":
        return run_smoke()
    return run_pipeline(full=args.full, verbose=args.verbose, open_report=args.open)


if __name__ == "__main__":
    sys.exit(main())
