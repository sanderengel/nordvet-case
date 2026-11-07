"""Tests for the Parquet export that need no database: types, the SELECT, writing and reading back."""

from datetime import date, datetime
from decimal import Decimal

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from nordvet.model.export import VIEWS, Column, arrow_type, select_sql, to_table, write_all  # noqa: E402

COLUMNS = [
    Column("appointment_id", "varchar"),
    Column("duration_minutes", "int"),
    Column("scheduled_date", "date"),
    Column("scheduled_at", "datetimeoffset"),
    Column("created_at_utc", "datetime2"),
    Column("cancelled_after_start", "bit"),
    Column("no_show_rate", "decimal", 9, 6),
]
ROWS = [
    ("apt_1", 20, date(2026, 11, 5), datetime(2026, 11, 5, 9, 0), datetime(2026, 11, 1, 8, 0), 1, Decimal("0.750000")),
    ("apt_2", 15, date(2026, 11, 6), datetime(2026, 11, 6, 9, 0), datetime(2026, 11, 2, 8, 0), 0, None),
]


def test_the_export_covers_the_star_schema_plus_clinic_day_and_as_of():  # D-045
    assert set(VIEWS) == {"fact_appointment", "dim_clinic", "dim_date", "dim_service", "clinic_day", "as_of"}


def test_sql_types_map_to_parquet_types():
    assert [str(arrow_type(c)) for c in COLUMNS] == [
        "string", "int32", "date32[day]", "timestamp[s]", "timestamp[s]", "bool", "decimal128(9, 6)",
    ]
    with pytest.raises(ValueError):
        arrow_type(Column("x", "geography"))


def test_datetimeoffset_is_selected_as_local_wall_time():
    sql = select_sql("fact_appointment", COLUMNS)
    assert "CAST([scheduled_at] AS DATETIME2(0)) AS [scheduled_at]" in sql
    assert "[created_at_utc]" in sql and "CAST([created_at_utc]" not in sql
    assert sql.endswith("FROM marts.[fact_appointment]")


def test_a_column_of_only_nulls_keeps_its_type():
    table = to_table(COLUMNS, [r[:-1] + (None,) for r in ROWS])
    assert str(table.schema.field("no_show_rate").type) == "decimal128(9, 6)"
    assert table.column("cancelled_after_start").to_pylist() == [True, False]


def test_files_are_written_and_read_back(tmp_path):
    written = write_all({"fact_appointment": to_table(COLUMNS, ROWS)}, tmp_path)
    assert [p.name for p in written] == ["fact_appointment.parquet"]
    assert not list(tmp_path.glob(".*.tmp"))
    back = pq.read_table(tmp_path / "fact_appointment.parquet")
    assert back.num_rows == 2
    assert back.column("no_show_rate").to_pylist() == [Decimal("0.750000"), None]
    assert back.column("scheduled_at").to_pylist()[0] == datetime(2026, 11, 5, 9, 0)


def test_a_failed_export_leaves_the_previous_files(tmp_path):
    write_all({"dim_service": to_table([Column("service", "varchar")], [("dental",)])}, tmp_path)

    class Broken:
        pass  # not a table: writing it fails

    with pytest.raises(Exception):
        write_all({"dim_service": to_table([Column("service", "varchar")], [("surgery",)]), "clinic_day": Broken()},
                  tmp_path)
    assert pq.read_table(tmp_path / "dim_service.parquet").column("service").to_pylist() == ["dental"]
    assert not list(tmp_path.glob(".*.tmp"))
