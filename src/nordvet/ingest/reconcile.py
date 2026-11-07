"""Live reconciliation (Phase 3.2 step 4, D-028, Q12): does staging built over several
incremental runs equal staging from a fresh full load?

    python -m nordvet.ingest.load                                          # incremental runs, over time
    MSSQL_DATABASE=nordvet_fresh python -m nordvet.ingest.load --full      # a fresh full load, later
    python -m nordvet.ingest.reconcile

It only reads: it runs the three queries in sql/checks/reconcile_*.sql (the database names
nordvet and nordvet_fresh are fixed in them) and prints the verdict. A difference is explained
when the fresh load holds a newer version of a row that was written after the last incremental
run began; every other difference is a failure. Exit codes: 0 pass, 1 configuration, 2 failed.
"""

from __future__ import annotations

import sys

from nordvet.config import ConfigError, load_settings, redact
from nordvet.db.connection import connect
from nordvet.ingest.sqlfiles import read_batches

MAX_SHOWN = 15
EXIT_OK, EXIT_CONFIG, EXIT_FAILED = 0, 1, 2


def split_differences(rows: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    """(explained, unexplained) from the rows of reconcile_appointments.sql; explained is the last column."""
    return [r for r in rows if r[-1]], [r for r in rows if not r[-1]]


def _query(conn, path: str) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(read_batches(path)[0])
    return [tuple(r) for r in cur.fetchall()]


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return EXIT_CONFIG
    if settings.missing():
        print("Missing in the env file: " + ", ".join(settings.missing()))
        return EXIT_CONFIG

    try:
        conn = connect(settings, database="master")
        try:
            counts = _query(conn, "checks/reconcile_counts.sql")
            appointments = _query(conn, "checks/reconcile_appointments.sql")
            clinics = _query(conn, "checks/reconcile_clinics.sql")
        finally:
            conn.close()
    except Exception as exc:
        print(f"Reconciliation failed to run: {type(exc).__name__}: {redact(exc, settings)}")
        return EXIT_FAILED

    for what, value in counts:
        print(f"{what}: {value}")
    explained, unexplained = split_differences(appointments)
    print(f"\nappointments that differ: {len(appointments)} "
          f"(explained: {len(explained)}, unexplained: {len(unexplained)})")
    for row in (unexplained + explained)[:MAX_SHOWN]:
        kind, id_, inc_status, fresh_status, inc_updated, fresh_updated, ok = row
        print(f"  {'explained  ' if ok else 'UNEXPLAINED'} {kind:<16} {id_}  "
              f"{inc_status} ({inc_updated}) -> {fresh_status} ({fresh_updated})")
    if len(appointments) > MAX_SHOWN:
        print(f"  ... and {len(appointments) - MAX_SHOWN} more")
    print(f"clinics that differ: {len(clinics)}")
    for id_, kind in clinics[:MAX_SHOWN]:
        print(f"  {kind:<16} {id_}")

    passed = not unexplained and not clinics
    print(f"\nRESULT: {'PASS' if passed else 'FAIL'}")
    return EXIT_OK if passed else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
