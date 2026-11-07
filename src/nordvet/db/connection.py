"""SQL Server connections (T-SQL, via mssql-python)."""

from __future__ import annotations

import logging
import time
from typing import Callable

from nordvet.config import Settings

log = logging.getLogger("nordvet.db")


def connection_string(settings: Settings, database: str | None = None) -> str:
    """ODBC-style connection string. Contains the password: never print it."""
    parts = [
        f"SERVER={settings.mssql_host},{settings.mssql_port}",
        f"DATABASE={database or settings.mssql_database}",
        "UID=sa",
        f"PWD={settings.mssql_password}",
        "Encrypt=yes",
        "TrustServerCertificate=yes",  # local container with a self-signed certificate
    ]
    return ";".join(parts)


def connect(settings: Settings, database: str | None = None, autocommit: bool = False):
    import mssql_python  # imported here so config and tests work without the driver

    return mssql_python.connect(connection_string(settings, database), autocommit=autocommit)


def ensure_database(settings: Settings) -> bool:
    """Create the pipeline database if it does not exist. Returns True if it was created."""
    # The name was validated in config.load_settings, so interpolation is safe here.
    name = settings.mssql_database
    conn = connect(settings, database="master", autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT DB_ID(N'{name}')")
        exists = cur.fetchone()[0] is not None
        if not exists:
            cur.execute(f"CREATE DATABASE [{name}]")
        return not exists
    finally:
        conn.close()


def ensure_database_with_retry(
    settings: Settings,
    tries: int = 12,
    wait_s: float = 5,
    sleep: Callable[[float], None] = time.sleep,
    ensure: Callable[[Settings], bool] = ensure_database,
) -> bool:
    """ensure_database, trying again for about a minute.

    A freshly started SQL Server container under Rosetta needs 10-30 seconds before it takes
    connections; before that the driver fails with "error during handshakes before login".
    Only the error's type name is logged, never its text.
    """
    for attempt in range(1, tries + 1):
        try:
            return ensure(settings)
        except Exception as exc:
            if attempt == tries:
                raise
            log.warning("database not ready (%s), waiting %ss, try %d/%d", type(exc).__name__, wait_s, attempt, tries)
            sleep(wait_s)
    raise AssertionError("unreachable")
