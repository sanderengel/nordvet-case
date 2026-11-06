"""SQL Server connections (T-SQL, via mssql-python)."""

from __future__ import annotations

from nordvet.config import Settings


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
