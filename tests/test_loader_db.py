"""The loader's acceptance tests, against the fake API and a real SQL Server: a second run with no
change leaves staging identical, a row written during a run arrives with the next run, and a failed
run leaves nothing behind.

They run in a separate database, nordvet_test, which the tests create and clear themselves;
the real nordvet database is never touched. If the env file or SQL Server is not there (no
Docker, a CI machine), the tests are skipped instead of failing.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import pytest

from fake_api import FakeApi, appt
from nordvet.api.client import ApiError
from nordvet.config import ConfigError, load_settings
from nordvet.db.connection import connect, ensure_database
from nordvet.ingest import load
from nordvet.ingest.fetch import fetch_run
from nordvet.ingest.raw import create_tables, write_run
from nordvet.ingest.sqlfiles import run_file

TEST_DB = "nordvet_test"
RUN_ONE = datetime(2026, 11, 7, 12, 0, 0)


@pytest.fixture(scope="module")
def test_settings():
    try:
        s = load_settings()
    except ConfigError as exc:  # the message never contains a secret
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
    create_tables(c)  # makes sure the schemas exist before the reset drops tables
    run_file(c, "raw/reset_dev.sql")
    c.commit()
    yield c
    c.close()


# ----------------------------------------------------------------------------- helpers


def dataset():
    return [
        appt("apt_000000000001", "2026-08-06T09:00+02:00", "2026-08-06T07:00:00Z", "completed"),
        appt("apt_000000000002", "2026-10-30T08:00+01:00", "2026-10-30T10:00:00Z", "no_show"),
        appt("apt_000000000003", "2026-11-05T09:00+01:00", "2026-11-05T11:00:00Z", "completed"),
        appt("apt_000000000004", "2026-11-05T09:15+01:00", "2026-11-07T09:00:00Z", "booked"),
        appt("apt_000000000005", "2026-11-09T09:00+01:00", "2026-11-07T11:30:00Z", "cancelled"),
        appt("apt_000000000006", "2026-12-01T09:00+01:00", "2026-11-06T09:00:00Z", "booked",
             deleted="2026-11-06T09:00:00Z"),
    ]


def clock(api):
    return lambda: api.now.replace(tzinfo=None, microsecond=0)


def rows(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    return [tuple(r) for r in cur.fetchall()]


def staging_state(conn):
    appointments = rows(
        conn,
        "SELECT id, clinic_id, vet_id, service, CONVERT(VARCHAR(33), scheduled_at, 127), duration_minutes, "
        "status, created_at, updated_at, cancelled_at, deleted_at FROM staging.appointment ORDER BY id",
    )
    clinics = rows(conn, "SELECT id, name, city, country, timezone, active, opened_on FROM staging.clinic ORDER BY id")
    return appointments, clinics


def everything(conn):
    """What a failed run must not change: raw, the run record, the watermark, staging."""
    return (
        rows(conn, "SELECT COUNT(*) FROM raw.run"),
        rows(conn, "SELECT COUNT(*) FROM raw.page"),
        load.last_api_run_start(conn),
        staging_state(conn),
    )


# ----------------------------------------------------------------------------- the tests


def test_a_second_run_with_no_change_at_the_source_leaves_staging_identical(conn):
    api = FakeApi(dataset())
    first = load.run_load(conn, api, now=clock(api))
    assert first.full and first.appointment_rows == 6
    before = staging_state(conn)
    assert len(before[0]) == 6 and len(before[1]) == 1  # all rows, the deleted one too

    api.now += timedelta(minutes=30)
    api.calls.clear()
    second = load.run_load(conn, api, now=clock(api))

    assert not second.full
    assert second.updated_since == "2026-11-07T11:50:00Z"  # run start minus 10 minutes
    assert second.appointment_rows == 0  # nothing changed since then
    assert staging_state(conn) == before
    assert all(n == 0 for _, _, _, n in second.changes)
    asked = [p for path, p in api.calls if path == "/v1/appointments"]
    assert asked and all(p["updated_since"] == "2026-11-07T11:50:00Z" for p in asked)
    assert rows(conn, "SELECT COUNT(*) FROM raw.run") == [(2,)]


def test_a_row_written_during_a_run_is_picked_up_by_the_next_run(conn):
    api = FakeApi(dataset(), max_page=1)  # one row per page, so a run takes several calls
    # Both are written while the run is in progress, after the first November page was served.
    # A sorts before the cursor (missed by this run); B sorts after it (seen by this run) and
    # has the later stamp, as a write that happens a little later does.
    row_a = appt("apt_00000000000a", "2026-11-02T09:00+01:00", "2026-11-07T12:00:00Z")
    row_b = appt("apt_00000000000b", "2026-11-20T09:00+01:00", "2026-11-07T12:01:00Z")
    original = api.get
    written = []

    def get(path, params=None):
        resp = original(path, params)
        if path == "/v1/appointments" and params["from"] == "2026-11-01" and "cursor" not in params and not written:
            api.appointments += [row_a, row_b]
            written.append(True)
        return resp

    api.get = get
    load.run_load(conn, api, now=clock(api))
    ids = {r[0] for r in staging_state(conn)[0]}
    assert "apt_00000000000b" in ids and "apt_00000000000a" not in ids  # A was missed by run 1

    # A watermark taken from the newest updated_at seen would now skip A for good.
    newest = rows(conn, "SELECT MAX(updated_at) FROM staging.appointment")[0][0]
    assert newest == datetime(2026, 11, 7, 12, 1, 0)
    wrong = original(
        "/v1/appointments",
        {"from": "2026-11-01", "to": "2026-11-30", "include_deleted": "true",
         "updated_since": newest.strftime("%Y-%m-%dT%H:%M:%SZ")},
    )
    assert "apt_00000000000a" not in wrong.text

    # Run start (12:00:00) minus 10 minutes does not.
    api.now += timedelta(minutes=5)
    load.run_load(conn, api, now=clock(api))
    ids = {r[0] for r in staging_state(conn)[0]}
    assert "apt_00000000000a" in ids


def test_a_run_that_fails_while_fetching_leaves_no_trace(conn):
    api = FakeApi(dataset())
    load.run_load(conn, api, now=clock(api))
    before = everything(conn)

    api.now += timedelta(minutes=30)
    original = api.get
    calls = []

    def flaky(path, params=None):
        calls.append(path)
        if len(calls) == 4:  # clinics and two months went fine, the third month fails
            raise ApiError("the API went away")
        return original(path, params)

    api.get = flaky
    with pytest.raises(ApiError):
        load.run_load(conn, api, now=clock(api))

    assert len(calls) == 4
    assert everything(conn) == before
    assert load.last_api_run_start(conn) == RUN_ONE  # the watermark did not move


def test_a_run_that_fails_while_writing_leaves_no_trace(conn):
    api = FakeApi(dataset(), max_page=1)
    load.run_load(conn, api, now=clock(api))
    before = everything(conn)

    api.now += timedelta(minutes=30)
    result = fetch_run(api, now=clock(api))
    assert len(result.pages) > 3
    broken = dataclasses.replace(result.pages[-1], body=None)  # raw.page.body is NOT NULL
    with pytest.raises(Exception):
        write_run(conn, "api", result.pages[:-1] + [broken], started_at=result.started_at)

    assert everything(conn) == before  # the run record and the earlier pages were rolled back too
