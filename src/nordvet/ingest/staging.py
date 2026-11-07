"""Building the typed staging tables from raw: one row per id, the latest version."""

from __future__ import annotations

from nordvet.ingest.sqlfiles import run_file

BUILD_FILES = ("staging/010_build_appointment.sql", "staging/020_build_clinic.sql")


def build_staging(conn) -> None:
    """Rebuild staging from all of raw, in one transaction (a failure leaves the old tables)."""
    try:
        for path in BUILD_FILES:
            run_file(conn, path)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
