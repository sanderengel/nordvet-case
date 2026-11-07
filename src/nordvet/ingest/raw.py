"""Writing API responses to the raw layer (D-024, D-027).

A run's pages and its run record are inserted in ONE transaction: either all of it
is there, or none of it. Nothing is parsed here; the body is stored as received.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from nordvet.ingest.sqlfiles import run_file

SCHEMA_FILES = ("raw/001_tables.sql", "staging/001_tables.sql")


@dataclass(frozen=True)
class Page:
    """One saved API response."""

    endpoint: str  # 'appointments' or 'clinics'
    params: dict  # request parameters, WITHOUT the cursor
    page_no: int  # 1 = first page of that request sequence
    server_date: datetime  # the response's Date header, naive UTC, whole seconds
    fetched_at: datetime  # when we received it, naive UTC, whole seconds
    body: str  # the JSON text as received
    rows: int = 0  # number of appointments in the body (0 for clinics)


def create_tables(conn) -> None:
    for path in SCHEMA_FILES:
        run_file(conn, path)
    conn.commit()


def write_run(conn, source: str, pages: list[Page]) -> int:
    """Insert one run record and all its pages in one transaction. Returns the run id."""
    if not pages:
        raise ValueError("a run needs at least one page")
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO raw.run (started_at, finished_at, pages, appointment_rows, source) "
            "OUTPUT INSERTED.run_id VALUES (?, ?, ?, ?, ?)",
            (
                pages[0].server_date,
                max(p.fetched_at for p in pages),
                len(pages),
                sum(p.rows for p in pages),
                source,
            ),
        )
        run_id = cur.fetchone()[0]
        for p in pages:
            cur.execute(
                "INSERT INTO raw.page (run_id, endpoint, params, page_no, server_date, fetched_at, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    p.endpoint,
                    json.dumps(p.params, sort_keys=True),
                    p.page_no,
                    p.server_date,
                    p.fetched_at,
                    p.body,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return run_id
