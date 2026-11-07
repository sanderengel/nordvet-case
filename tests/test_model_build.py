"""Tests for the marts build that need no database: file order, the SQL itself, the pass rule."""

import re

from nordvet.ingest.sqlfiles import SQL_DIR, read_batches
from nordvet.model.build import CHECKS_FILE, CheckResult, failed, mart_files

VIEW = re.compile(r"CREATE\s+OR\s+ALTER\s+VIEW\s+marts\.(\w+)", re.IGNORECASE)
REFERENCE = re.compile(r"\bmarts\.(\w+)", re.IGNORECASE)


def code(path: str) -> str:
    """The file without its -- comments."""
    text = (SQL_DIR / path).read_text(encoding="utf-8")
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def test_mart_files_run_in_file_name_order_and_start_with_the_schema():
    files = mart_files()
    assert files == sorted(files)
    assert files[0] == "marts/001_schema.sql"
    assert all(f.startswith("marts/") and f.endswith(".sql") for f in files)


def test_every_view_file_defines_one_view_and_uses_only_views_defined_before_it():
    defined: set[str] = set()
    for path in mart_files()[1:]:
        sql = code(path)
        views = VIEW.findall(sql)
        assert len(views) == 1, path
        used = {name for name in REFERENCE.findall(sql)} - {views[0]}
        assert used <= defined, f"{path} uses {used - defined} before it is created"
        defined.add(views[0])
    assert {"fact_appointment", "clinic_day", "as_of", "dim_date", "dim_clinic", "dim_service"} <= defined


def test_the_marts_carry_neither_vet_id_nor_patient_ref():
    # D-019 / D-036: no vet-level metrics; D-027: patient_ref stays in raw.
    for path in mart_files():
        sql = code(path).lower()
        assert "vet_id" not in sql and "patient_ref" not in sql, path


def test_soft_deleted_rows_are_filtered_out_of_the_fact():
    assert "deleted_at is null" in code("marts/100_fact_appointment.sql").lower()


def test_the_checks_file_is_one_query():
    assert len(read_batches(CHECKS_FILE)) == 1


def test_only_fail_checks_with_problems_fail_the_build():
    results = [
        CheckResult("a", "fail", 0, ""),
        CheckResult("b", "info", 944, "944 soft-deleted"),
        CheckResult("c", "fail", 3, ""),
    ]
    assert [r.name for r in failed(results)] == ["c"]
    assert failed(results[:2]) == []
