"""Reading and running the .sql files under sql/.

Files are split into batches on lines that hold only GO (the way sqlcmd does), because
CREATE SCHEMA and similar statements must be alone in their batch. GO is not T-SQL: the
server never sees it.
"""

from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[3] / "sql"
_GO = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)


def split_batches(text: str) -> list[str]:
    """Split a script into batches at GO lines; empty batches are dropped."""
    return [batch.strip() for batch in _GO.split(text) if batch.strip()]


def read_batches(relative_path: str, sql_dir: Path = SQL_DIR) -> list[str]:
    return split_batches((sql_dir / relative_path).read_text(encoding="utf-8"))


def run_file(conn, relative_path: str, sql_dir: Path = SQL_DIR) -> None:
    """Run every batch of one file on this connection. The caller commits."""
    cur = conn.cursor()
    for batch in read_batches(relative_path, sql_dir):
        cur.execute(batch)
