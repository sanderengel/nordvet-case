"""Building the marts: views on top of staging, created by one command.

    python -m nordvet.model.build [-v]

  1. wait for the database (about a minute, as the loader does);
  2. run every file in sql/marts/ in file-name order, in ONE transaction: the schema, then the
     views (CREATE OR ALTER VIEW). A failure rolls back, so the views stay as they were;
  3. run the build checks in sql/checks/marts_checks.sql and print them.

The views read staging and raw.run, so load first (python -m nordvet.ingest.load). The build
does not create the database: a missing database or missing staging tables is a failed build.

Exit codes: 0 built and every check passed, 1 configuration problem, 2 the build failed
or a check with severity 'fail' found problems.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from nordvet.config import ConfigError, load_settings, redact
from nordvet.db.connection import connect, ensure_database_with_retry
from nordvet.ingest.sqlfiles import SQL_DIR, read_batches, run_file

log = logging.getLogger("nordvet.model")

MARTS_DIR = "marts"
CHECKS_FILE = "checks/marts_checks.sql"
EXIT_OK, EXIT_CONFIG, EXIT_FAILED = 0, 1, 2


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: str  # 'fail' or 'info'
    problems: int
    note: str

    @property
    def failed(self) -> bool:
        return self.severity == "fail" and self.problems > 0


def mart_files(sql_dir: Path = SQL_DIR) -> list[str]:
    """The files under sql/marts/, relative to sql/, in the order they must run (by file name)."""
    return [f"{MARTS_DIR}/{p.name}" for p in sorted((sql_dir / MARTS_DIR).glob("*.sql"))]


def create_views(conn, files: list[str] | None = None) -> None:
    """Run the marts files in one transaction; on any error nothing is changed."""
    try:
        for path in files if files is not None else mart_files():
            log.debug("running %s", path)
            run_file(conn, path)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def run_checks(conn) -> list[CheckResult]:
    cur = conn.cursor()
    cur.execute(read_batches(CHECKS_FILE)[0])
    return [CheckResult(str(n), str(s), int(p), str(note or "")) for n, s, p, note in cur.fetchall()]


def failed(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.failed]


def build(conn) -> list[CheckResult]:
    """Create or replace the views, then run the checks. Returns every check result."""
    files = mart_files()
    create_views(conn, files)
    log.info("views built from %d files in sql/%s", len(files), MARTS_DIR)
    return run_checks(conn)


def _reachable(settings) -> bool:
    connect(settings).close()  # raises until the server takes connections
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet.model.build", description="Build the marts views and run the checks")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging, with tracebacks")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return EXIT_CONFIG
    if settings.missing():
        log.error("missing in the env file: %s", ", ".join(settings.missing()))
        return EXIT_CONFIG

    conn = None
    try:
        ensure_database_with_retry(settings, ensure=_reachable)
        conn = connect(settings)
        results = build(conn)
    except Exception as exc:
        log.error("build failed: %s: %s", type(exc).__name__, redact(exc, settings))
        log.debug("traceback", exc_info=True)
        return EXIT_FAILED
    finally:
        if conn is not None:
            conn.close()

    for r in results:
        mark = "FAIL" if r.failed else ("info" if r.severity == "info" else "ok  ")
        log.info("check %s %-36s %6d  %s", mark, r.name, r.problems, r.note)
    bad = failed(results)
    if bad:
        log.error("build checks failed: %s", ", ".join(r.name for r in bad))
        return EXIT_FAILED
    log.info("done: %d checks, all passed", len(results))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
