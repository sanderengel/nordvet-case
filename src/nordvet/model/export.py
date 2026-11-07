"""Export the marts as Parquet files (Phase 4, D-041, D-045, D-046).

    python -m nordvet.model.export [--out exports] [-v]

Writes one file per view to exports/<view>.parquet, overwriting the previous export (D-046):
the star schema (fact_appointment, dim_clinic, dim_date, dim_service), the clinic-day view the
report reads (clinic_day), and as_of, which says what the data is as of (D-045). Build first
(python -m nordvet.model.build); the export only reads the views.

Column types come from the views' SQL types (INFORMATION_SCHEMA), not from the values, so a
column that happens to be all NULL still has its proper type. DATETIMEOFFSET columns
(scheduled_at, scheduled_end_at, as_of_local) are written as the clinic's local wall time without
an offset: a Parquet column holds one time zone, and Danish and Swedish rows can carry +01:00 or
+02:00. The UTC stamps (*_utc) are written as they are, also without a zone.

Every file is first written to a temporary name and only renamed once all of them are written,
so a failed export leaves the previous files in place.

Exit codes (D-033): 0 ok, 1 configuration (also: pyarrow not installed), 2 the export failed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from nordvet.config import ConfigError, load_settings, redact
from nordvet.db.connection import connect, ensure_database_with_retry

log = logging.getLogger("nordvet.export")

VIEWS = ("fact_appointment", "dim_clinic", "dim_date", "dim_service", "clinic_day", "as_of")
DEFAULT_OUT = Path(__file__).resolve().parents[3] / "exports"
EXIT_OK, EXIT_CONFIG, EXIT_FAILED = 0, 1, 2

COLUMNS_SQL = (
    "SELECT COLUMN_NAME, DATA_TYPE, NUMERIC_PRECISION, NUMERIC_SCALE "
    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'marts' AND TABLE_NAME = ? "
    "ORDER BY ORDINAL_POSITION"
)


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    precision: int | None = None
    scale: int | None = None


def arrow_type(col: Column):
    """The Parquet (Arrow) type for one SQL Server column type of the marts."""
    import pyarrow as pa

    t = col.sql_type.lower()
    if t in ("varchar", "nvarchar", "char", "nchar"):
        return pa.string()
    if t in ("int", "smallint", "tinyint"):
        return pa.int32()
    if t == "bigint":
        return pa.int64()
    if t == "bit":
        return pa.bool_()
    if t == "date":
        return pa.date32()
    if t in ("datetime2", "datetime", "datetimeoffset"):  # datetimeoffset: selected as local wall time
        return pa.timestamp("s")
    if t in ("decimal", "numeric"):
        return pa.decimal128(int(col.precision), int(col.scale))
    raise ValueError(f"no Parquet type for column {col.name} of SQL type {col.sql_type}")


def select_sql(view: str, columns: list[Column]) -> str:
    """SELECT for one view; DATETIMEOFFSET columns come out as local wall time (DATETIME2)."""
    parts = []
    for c in columns:
        if c.sql_type.lower() == "datetimeoffset":
            parts.append(f"CAST([{c.name}] AS DATETIME2(0)) AS [{c.name}]")
        else:
            parts.append(f"[{c.name}]")
    return f"SELECT {', '.join(parts)} FROM marts.[{view}]"


def to_table(columns: list[Column], rows: list[tuple]):
    """An Arrow table with the column types of the view, whatever the values are."""
    import pyarrow as pa

    schema = pa.schema([pa.field(c.name, arrow_type(c)) for c in columns])
    data = {}
    for i, c in enumerate(columns):
        values = [r[i] for r in rows]
        if c.sql_type.lower() == "bit":
            values = [None if v is None else bool(v) for v in values]
        data[c.name] = values
    return pa.Table.from_pydict(data, schema=schema)


def write_all(tables: dict[str, object], out: Path) -> list[Path]:
    """Write every table to <out>/<view>.parquet: all to temporary names first, then rename."""
    import pyarrow.parquet as pq

    out.mkdir(parents=True, exist_ok=True)
    temps = []
    try:
        for view, table in tables.items():
            tmp = out / f".{view}.parquet.tmp"
            pq.write_table(table, tmp)
            temps.append((tmp, out / f"{view}.parquet"))
    except Exception:
        for tmp, _ in temps:
            tmp.unlink(missing_ok=True)
        raise
    for tmp, final in temps:
        os.replace(tmp, final)
    return [final for _, final in temps]


def read_view(conn, view: str):
    cur = conn.cursor()
    cur.execute(COLUMNS_SQL, (view,))
    columns = [Column(n, t, p, s) for n, t, p, s in cur.fetchall()]
    if not columns:
        raise RuntimeError(f"marts.{view} does not exist; run python -m nordvet.model.build first")
    cur.execute(select_sql(view, columns))
    return to_table(columns, [tuple(r) for r in cur.fetchall()])


def export(conn, out: Path = DEFAULT_OUT) -> dict[str, int]:
    """Read every view and write the files. Returns rows per view."""
    tables = {view: read_view(conn, view) for view in VIEWS}
    write_all(tables, out)
    return {view: t.num_rows for view, t in tables.items()}


def _reachable(settings) -> bool:
    connect(settings).close()
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet.model.export", description="Export the marts as Parquet")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="folder for the files (default: exports/)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging, with tracebacks")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        log.error("pyarrow is not installed: pip install -r requirements.txt")
        return EXIT_CONFIG
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return EXIT_CONFIG
    if not settings.mssql_password:
        log.error("missing in the env file: MSSQL_SA_PASSWORD")
        return EXIT_CONFIG

    conn = None
    t0 = time.monotonic()
    try:
        ensure_database_with_retry(settings, ensure=_reachable)
        conn = connect(settings)
        counts = export(conn, args.out)
    except Exception as exc:
        log.error("export failed: %s: %s", type(exc).__name__, redact(exc, settings))
        log.debug("traceback", exc_info=True)
        return EXIT_FAILED
    finally:
        if conn is not None:
            conn.close()
    for view, n in counts.items():
        log.info("wrote %s/%s.parquet: %d rows", args.out, view, n)
    log.info("done: %d files in %.1fs", len(counts), time.monotonic() - t0)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
