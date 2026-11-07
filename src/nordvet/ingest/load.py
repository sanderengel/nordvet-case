"""The load run: from the API into raw, then staging.

    python -m nordvet.ingest.load [--full] [-v]

One run, in this order:
  1. wait for the database, make sure the tables exist;
  2. find the start of the last API run in raw.run; no such run, or --full, means a full load;
  3. fetch every page from the API into memory (nothing is written yet);
  4. write all pages and the run record in ONE transaction;
  5. rebuild staging from all of raw and log what changed.

If anything fails before step 4 ends, the database is as it was, so the next run asks for the
same rows again. If step 5 fails, raw and the run record are already in; staging is rebuilt from
all of raw, so the next run repairs it.

Exit codes: 0 ok, 1 configuration problem, 2 the run failed.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from nordvet.api.client import ApiClient
from nordvet.config import ConfigError, load_settings, redact
from nordvet.db.connection import connect, ensure_database_with_retry
from nordvet.ingest.fetch import DEFAULT_START, Getter, fetch_run, updated_since_param, utc_now
from nordvet.ingest.raw import create_tables, write_run
from nordvet.ingest.sqlfiles import read_batches, run_file
from nordvet.ingest.staging import build_staging

log = logging.getLogger("nordvet.load")

API_SOURCE = "api"  # raw.run.source of runs that came from the API; only these set the watermark
EXIT_OK, EXIT_CONFIG, EXIT_FAILED = 0, 1, 2


@dataclass(frozen=True)
class LoadSummary:
    run_id: int
    full: bool
    updated_since: str | None
    pages: int
    appointment_rows: int
    restarts: int
    started_at: datetime
    changes: list[tuple]  # (kind, from_status, to_status, n) rows of the changes query


def last_api_run_start(conn) -> datetime | None:
    cur = conn.cursor()
    cur.execute(read_batches("raw/030_last_api_run.sql")[0], (API_SOURCE,))
    return cur.fetchone()[0]


def run_load(
    conn,
    client: Getter,
    *,
    full: bool = False,
    start=DEFAULT_START,
    now: Callable[[], datetime] = utc_now,
) -> LoadSummary:
    """One load run on an open connection with a client. Both are parameters so that the tests
    can run it against the fake API and a test database."""
    create_tables(conn)

    last = None if full else last_api_run_start(conn)
    since = updated_since_param(last) if last else None
    if since:
        log.info("incremental run: updated_since %s (last run started %s, minus the margin)", since, last)
    else:
        log.info("full run: no updated_since (%s)", "--full" if full else "no earlier API run")

    t0 = time.monotonic()
    result = fetch_run(client, updated_since=since, start=start, now=now)
    rows = sum(p.rows for p in result.pages)
    log.info(
        "fetched %d pages, %d appointment rows in %.0fs (%d month restarts)",
        len(result.pages), rows, time.monotonic() - t0, result.restarts,
    )

    # Step 4: raw pages and the run record, one transaction. The temp snapshot of staging for
    # the change report is taken first; it only lives on this connection.
    run_file(conn, "staging/005_snapshot_before.sql")
    run_id = write_run(conn, API_SOURCE, result.pages, started_at=result.started_at)
    log.info("raw: run %d written (%d pages)", run_id, len(result.pages))

    build_staging(conn)
    changes = _changes(conn)
    _log_changes(changes)
    return LoadSummary(run_id, full or last is None, since, len(result.pages), rows, result.restarts,
                       result.started_at, changes)


def _changes(conn) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(read_batches("staging/030_changes_since_snapshot.sql")[0])
    return [tuple(r) for r in cur.fetchall()]


def _log_changes(changes: list[tuple]) -> None:
    """Report new rows, status changes and new soft deletions.

    booked to no_show is the usual late change: clinics record no-shows a day or more later.
    """
    parts = []
    for kind, from_status, to_status, n in sorted(changes, key=lambda c: (c[0], str(c[1]), str(c[2]))):
        if not n:
            continue
        if kind == "status":
            parts.append(f"{from_status} -> {to_status}: {n}")
        else:
            parts.append(f"{kind}: {n}")
    log.info("staging changed: %s", "; ".join(parts) if parts else "nothing")
    if any(kind == "gone" and n for kind, _, _, n in changes):
        log.warning("some appointment ids disappeared from staging; raw is append-only, so this is a bug")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet.ingest.load", description="Load the VetOne API into SQL Server")
    parser.add_argument("--full", action="store_true", help="ignore earlier runs and load everything")
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
        ensure_database_with_retry(settings)
        conn = connect(settings)
        client = ApiClient(settings.api_base_url, settings.api_client_id, settings.api_client_secret)
        summary = run_load(conn, client, full=args.full)
    except Exception as exc:
        log.error("run failed: %s: %s", type(exc).__name__, redact(exc, settings))
        log.debug("traceback", exc_info=True)
        return EXIT_FAILED
    finally:
        if conn is not None:
            conn.close()

    log.info(
        "done: run %d (%s), %d pages, %d appointment rows",
        summary.run_id, "full" if summary.full else "incremental", summary.pages, summary.appointment_rows,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
