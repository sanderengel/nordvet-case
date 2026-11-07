"""The marts against a real SQL Server (Phase 4, D-038): load a small known data set through the
fake API, build the views and check the outcomes, the flags and the clinic-day rates by hand.

Runs in the separate nordvet_test database (cleared by the tests themselves); skipped when the
env file or SQL Server is not there, like tests/test_loader_db.py.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal

import pytest

from fake_api import FakeApi, appt
from nordvet.config import ConfigError, load_settings
from nordvet.db.connection import connect, ensure_database
from nordvet.ingest import load
from nordvet.ingest.raw import create_tables
from nordvet.ingest.sqlfiles import run_file
from nordvet.model.build import build, failed

TEST_DB = "nordvet_test"

CLINICS = [
    {"id": "CL-001", "name": "Århus Test", "city": "Århus", "country": "DK",
     "timezone": "Europe/Copenhagen", "active": True, "opened_on": "2020-01-01"},
    {"id": "CL-002", "name": "Malmö Test", "city": "Malmö", "country": "SE",
     "timezone": "Europe/Stockholm", "active": False, "opened_on": "2020-01-01"},
]


def dataset():
    # The fake API's clock says 2026-11-07 12:00 UTC (13:00 local), so the as-of date is 7 Nov.
    return [
        appt("apt_000000000001", "2026-08-06T09:00+02:00", "2026-08-06T08:00:00Z", "completed"),
        appt("apt_000000000002", "2026-11-05T09:00+01:00", "2026-11-05T09:00:00Z", "completed"),
        appt("apt_000000000003", "2026-11-05T09:30+01:00", "2026-11-06T08:00:00Z", "no_show"),
        appt("apt_000000000004", "2026-11-05T10:00+01:00", "2026-11-04T08:00:00Z", "booked"),
        appt("apt_000000000005", "2026-11-05T10:30+01:00", "2026-11-04T08:00:00Z", "booked"),
        # cancelled 30 minutes after the start (09:00 local = 08:00 UTC), and one the day before
        appt("apt_000000000006", "2026-11-05T09:00+01:00", "2026-11-05T08:30:00Z", "cancelled",
             cancelled_at="2026-11-05T08:30:00Z"),
        appt("apt_000000000007", "2026-11-05T11:00+01:00", "2026-11-04T08:00:00Z", "cancelled",
             cancelled_at="2026-11-04T08:00:00Z"),
        # the as-of day: a recorded completion and one still booked
        appt("apt_000000000008", "2026-11-07T08:00+01:00", "2026-11-07T07:30:00Z", "completed"),
        appt("apt_000000000009", "2026-11-07T15:00+01:00", "2026-11-01T08:00:00Z", "booked"),
        # the future, and a soft-deleted row that must not reach the fact
        appt("apt_000000000010", "2026-11-20T09:00+01:00", "2026-11-01T08:00:00Z", "booked"),
        appt("apt_000000000011", "2026-11-05T12:00+01:00", "2026-11-06T09:00:00Z", "no_show",
             deleted="2026-11-06T09:00:00Z"),
        appt("apt_000000000012", "2026-11-05T09:00+01:00", "2026-11-05T09:30:00Z", "completed", clinic="CL-002"),
    ]


@pytest.fixture(scope="module")
def test_settings():
    try:
        s = load_settings()
    except ConfigError as exc:
        pytest.skip(f"database tests need the local setup: {exc}")
    if not s.mssql_password:
        pytest.skip("MSSQL_SA_PASSWORD is not set")
    return dataclasses.replace(s, mssql_database=TEST_DB)


@pytest.fixture
def conn(test_settings):
    assert test_settings.mssql_database == TEST_DB  # never reset any other database
    try:
        ensure_database(test_settings)
        c = connect(test_settings)
    except Exception as exc:
        pytest.skip(f"SQL Server is not reachable ({type(exc).__name__})")
    create_tables(c)
    run_file(c, "raw/reset_dev.sql")
    c.commit()
    yield c
    c.close()


def rows(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    return [tuple(r) for r in cur.fetchall()]


def loaded(conn, clinics=CLINICS):
    api = FakeApi(dataset(), clinics=clinics)
    load.run_load(conn, api, now=lambda: api.now.replace(tzinfo=None, microsecond=0))
    return build(conn)


def test_build_passes_its_checks_and_derives_the_outcomes(conn):
    results = loaded(conn)
    assert failed(results) == []
    info = {r.name: r.note for r in results if r.severity == "info"}
    assert info["soft_deleted_excluded"].startswith("1 ")

    outcomes = dict(rows(conn, "SELECT appointment_id, outcome FROM marts.fact_appointment"))
    assert outcomes == {
        "apt_000000000001": "completed",
        "apt_000000000002": "completed",
        "apt_000000000003": "no_show",
        "apt_000000000004": "no_show_assumed",
        "apt_000000000005": "no_show_assumed",
        "apt_000000000006": "cancelled",
        "apt_000000000007": "cancelled",
        "apt_000000000008": "completed",  # the as-of day keeps a recorded status (D-043)
        "apt_000000000009": "pending",
        "apt_000000000010": "pending",
        "apt_000000000012": "completed",
    }  # apt_000000000011 is soft-deleted and absent (D-015)

    flags = dict(rows(conn, "SELECT appointment_id, CAST(cancelled_after_start AS INT) "
                            "FROM marts.fact_appointment WHERE status = 'cancelled'"))
    assert flags == {"apt_000000000006": 1, "apt_000000000007": 0}
    notice = dict(rows(conn, "SELECT appointment_id, cancel_notice_minutes "
                             "FROM marts.fact_appointment WHERE status = 'cancelled'"))
    assert notice == {"apt_000000000006": -30, "apt_000000000007": 1560}  # 4 Nov 08:00 to 5 Nov 10:00 UTC


def test_each_clinic_is_as_of_the_latest_run_in_its_own_zone(conn):
    loaded(conn)
    as_of = rows(conn, "SELECT clinic_id, as_of_date, CAST(as_of_local AS DATETIME2(0)), "
                       "DATENAME(TZOFFSET, as_of_local) FROM marts.as_of ORDER BY clinic_id")
    local = datetime(2026, 11, 7, 13, 0, 0)  # the run started 12:00 UTC
    assert as_of == [
        ("CL-001", date(2026, 11, 7), local, "+01:00"),
        ("CL-002", date(2026, 11, 7), local, "+01:00"),
    ]


def test_clinic_day_counts_and_rates(conn):
    loaded(conn)
    day = rows(conn, """
        SELECT day_status, appointments, completed, no_show, no_show_assumed, cancelled,
               cancelled_after_start, pending, attended_or_missed, no_show_rate,
               confirmed_no_show_rate, CAST(is_assumed AS INT), CAST(is_active AS INT)
        FROM marts.clinic_day WHERE clinic_id = 'CL-001' AND calendar_date = '2026-11-05'""")
    # 1 completed, 1 no-show, 2 assumed: assumed rate 3/4, confirmed rate 1/2 (D-020, D-021)
    assert day == [("past", 6, 1, 1, 2, 2, 1, 0, 4, Decimal("0.750000"), Decimal("0.500000"), 1, 1)]

    as_of_day = rows(conn, """
        SELECT day_status, completed, pending, no_show_rate, confirmed_no_show_rate
        FROM marts.clinic_day WHERE clinic_id = 'CL-001' AND calendar_date = '2026-11-07'""")
    assert as_of_day == [("as_of_day", 1, 1, None, None)]  # no rate for the as-of day (D-021)

    empty = rows(conn, """
        SELECT appointments, no_show_rate FROM marts.clinic_day
        WHERE clinic_id = 'CL-001' AND calendar_date = '2026-11-06'""")
    assert empty == [(0, None)]  # nothing to divide: NULL, not 0% (D-039)

    closed = rows(conn, """
        SELECT CAST(is_active AS INT), completed, confirmed_no_show_rate FROM marts.clinic_day
        WHERE clinic_id = 'CL-002' AND calendar_date = '2026-11-05'""")
    assert closed == [(0, 1, Decimal("0.000000"))]  # kept in the mart, flagged (D-039)


def test_report_view_holds_clinics_with_appointments_past_days_and_the_30_day_rate(conn):
    loaded(conn)
    got = rows(conn, """
        SELECT clinic_id, calendar_date, attended_or_missed, no_show_rate, missed_30d,
               attended_or_missed_30d, no_show_rate_30d
        FROM marts.report_clinic_day
        WHERE clinic_id = 'CL-001' AND calendar_date IN ('2026-08-06', '2026-11-05', '2026-11-06')
        ORDER BY calendar_date""")
    # the 30 days are the days before the day itself (D-053, Phase 5 revision)
    assert got == [
        ("CL-001", date(2026, 8, 6), 1, Decimal("0.000000"), 0, 0, None),     # nothing before 6 Aug
        ("CL-001", date(2026, 11, 5), 4, Decimal("0.750000"), 0, 0, None),    # 6 Oct to 4 Nov: nothing
        # an empty day has no rate of its own; its previous 30 days hold 5 Nov (D-039)
        ("CL-001", date(2026, 11, 6), 0, None, 3, 4, Decimal("0.750000")),
    ]
    closed = rows(conn, "SELECT calendar_date FROM marts.report_clinic_day WHERE clinic_id = 'CL-002'")
    assert closed == [(date(2026, 11, 5),)]  # a closed clinic only on the days it had appointments (D-055)
    assert rows(conn, "SELECT MAX(calendar_date) FROM marts.report_clinic_day") == [(date(2026, 11, 6),)]  # D-021

    # the window covers 30 earlier days of data, or fewer after the first day with data (6 Aug
    # here; the date dimension starts on 1 Aug), in both views
    days = rows(conn, "SELECT calendar_date, days_30d FROM marts.report_clinic_day "
                      "WHERE clinic_id = 'CL-001' AND calendar_date IN ('2026-08-01', '2026-08-06', '2026-08-12', "
                      "'2026-11-05') ORDER BY calendar_date")
    assert days == [(date(2026, 8, 1), 0), (date(2026, 8, 6), 0), (date(2026, 8, 12), 6), (date(2026, 11, 5), 30)]
    assert rows(conn, "SELECT days_30d FROM marts.report_all_clinics_day "
                      "WHERE calendar_date IN ('2026-08-07', '2026-11-05') ORDER BY calendar_date") == [(1,), (30,)]

    total = rows(conn, """
        SELECT calendar_date, attended_or_missed, no_show_rate, no_show_rate_30d
        FROM marts.report_all_clinics_day
        WHERE calendar_date IN ('2026-08-06', '2026-11-05', '2026-11-06')
        ORDER BY calendar_date""")
    # 5 Nov: both clinics, 3 missed of 5 (D-056); 6 Nov's previous 30 days hold 5 Nov
    assert total == [
        (date(2026, 8, 6), 1, Decimal("0.000000"), None),
        (date(2026, 11, 5), 5, Decimal("0.600000"), None),
        (date(2026, 11, 6), 0, None, Decimal("0.600000")),
    ]


def test_the_report_is_written_from_the_view(conn, tmp_path):
    from nordvet.report.render import report

    loaded(conn)
    data = report(conn, tmp_path / "no-shows.html")
    assert data["days"][0]["date"] == "2026-11-05"  # 6 Nov had no appointments (D-052)
    assert data["defaultNote"].startswith("Friday 6 November 2026 had no appointments")
    assert data["days"][0]["rows"] == [
        ["Århus Test", "DK", 1, 1, 2, 4, "75.0%", True, "N/A", 0.75, None],
        ["Malmö Test", "SE", 1, 0, 0, 1, "0.0%", False, "N/A", 0.0, None],  # closed, but open that day
    ]
    assert data["days"][0]["total"] == ["All", "", 2, 1, 2, 5, "60.0%", True, "N/A", 0.6, None]
    assert data["days"][0]["windowLabel"] == "Previous 30 days"
    assert data["days"][-1] == {**data["days"][-1], "date": "2026-08-06", "windowLabel": "No earlier days"}
    assert data["asOf"] == "Saturday 7 November 2026, 13:00"
    assert (tmp_path / "no-shows.html").exists()


def test_date_dimension_runs_from_august_to_the_last_appointment(conn):
    loaded(conn)
    assert rows(conn, "SELECT MIN(calendar_date), MAX(calendar_date), COUNT(*) FROM marts.dim_date") == [
        (date(2026, 8, 1), date(2026, 11, 20), 112)
    ]
    sunday = rows(conn, "SELECT iso_weekday, weekday_name, iso_week, CAST(is_sunday AS INT) "
                        "FROM marts.dim_date WHERE calendar_date = '2026-10-25'")
    assert sunday == [(7, "Sunday", 43, 1)]


def test_a_clinic_in_an_unmapped_zone_fails_the_build(conn):
    oslo = [dict(CLINICS[0]), dict(CLINICS[1], timezone="Europe/Oslo")]
    names = {r.name for r in failed(loaded(conn, clinics=oslo))}
    assert "clinic_zone_unmapped" in names
    assert "fact_rows_differ_from_staging" in names  # its appointments drop out of the fact


def test_export_writes_every_view_with_its_types(conn, tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    from nordvet.model.export import export

    loaded(conn)
    counts = export(conn, tmp_path)
    assert counts == {"fact_appointment": 11, "dim_clinic": 2, "dim_date": 112, "dim_service": 1,
                      "clinic_day": 2 * 112, "as_of": 2}
    fact = pq.read_table(tmp_path / "fact_appointment.parquet")
    assert "vet_id" not in fact.column_names and "patient_ref" not in fact.column_names
    by_id = {r["appointment_id"]: r for r in fact.to_pylist()}
    assert by_id["apt_000000000002"]["scheduled_at"] == datetime(2026, 11, 5, 9, 0)  # local wall time
    assert by_id["apt_000000000006"]["cancelled_after_start"] is True
    assert str(fact.schema.field("scheduled_date").type) == "date32[day]"
    day = pq.read_table(tmp_path / "clinic_day.parquet").to_pylist()
    thu = next(r for r in day if r["clinic_id"] == "CL-001" and r["calendar_date"] == date(2026, 11, 5))
    assert thu["no_show_rate"] == Decimal("0.750000") and thu["is_assumed"] is True
    as_of = pq.read_table(tmp_path / "as_of.parquet").to_pylist()
    assert {r["as_of_local"] for r in as_of} == {datetime(2026, 11, 7, 13, 0)}
