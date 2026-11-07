"""Tests for the replay tool's pure parts and the SQL file reader. No network, no database."""

import json
from datetime import datetime

from nordvet.ingest.raw import Page
from nordvet.ingest.replay import (
    compare,
    expected_appointments,
    expected_clinics,
    find_clinics_page,
    norm_utc,
    pull_started_at,
    read_appointment_pages,
    split_scheduled,
)
from nordvet.ingest.sqlfiles import SQL_DIR, read_batches, split_batches


def row(id_="apt_000000000001", updated="2026-10-01T06:55:00Z", status="completed", **extra):
    base = {
        "id": id_, "clinic_id": "CL-316", "vet_id": "V3164", "patient_ref": "P3E6EDE22",
        "service": "follow_up", "scheduled_at": "2026-10-01T08:00+02:00", "duration_minutes": 15,
        "status": status, "created_at": "2026-09-30T10:34:11Z", "updated_at": updated,
    }
    base.update(extra)
    return base


def save(folder, name, params, rows, status=200, date="Fri, 06 Nov 2026 23:30:15 GMT", path="/v1/appointments"):
    """Write one saved response the way the Phase 2 tools do: body file plus meta file."""
    (folder / f"{name}.json").write_text(json.dumps({"data": rows, "next_cursor": None}), encoding="utf-8")
    (folder / f"{name}.meta.json").write_text(json.dumps({
        "path": path, "params": params, "status": status, "body_saved_as": f"{name}.json",
        "response_headers": {"date": date}, "fetched_at_utc": "2026-11-06T23:30:16+00:00",
    }), encoding="utf-8")


def test_split_batches_on_go_lines_only():
    text = "SELECT 1;\nGO\nSELECT 'GO now';\n  go  \n\nSELECT 3;\nGO\n"
    assert split_batches(text) == ["SELECT 1;", "SELECT 'GO now';", "SELECT 3;"]


def test_every_sql_file_has_batches_and_no_stray_go_text():
    for path in SQL_DIR.rglob("*.sql"):
        batches = read_batches(str(path.relative_to(SQL_DIR)))
        assert batches, path
        assert all("\nGO" not in b for b in batches), path


def test_read_pages_keeps_only_200_all_rows_pages_from_the_start_window(tmp_path):
    w = {"from": "2026-10-01", "to": "2026-11-01", "include_deleted": "true", "limit": 500}
    save(tmp_path, "0001_a", {**w}, [row()])
    save(tmp_path, "0002_b", {**w, "cursor": "SECRET.CURSOR"}, [row("apt_000000000002")])
    save(tmp_path, "0003_retry", {**w, "cursor": "x"}, [], status=429)  # an error answer
    save(tmp_path, "0004_normal", {"from": "2026-10-01", "to": "2026-11-01", "limit": 500}, [row()])  # not all-rows
    save(tmp_path, "0005_old", {**w, "from": "2025-06-01", "to": "2025-07-01"}, [])  # before the start
    save(tmp_path, "0006_other", {**w, "from": "2026-11-01", "to": "2026-12-01"}, [row("apt_000000000003")])

    pages = read_appointment_pages(tmp_path, start="2026-08-01")
    assert [p.page_no for p in pages] == [1, 2, 1]
    assert [p.rows for p in pages] == [1, 1, 1]
    assert all("cursor" not in p.params for p in pages)  # a cursor is never stored
    assert pages[0].server_date == datetime(2026, 11, 6, 23, 30, 15)
    assert pages[0].fetched_at == datetime(2026, 11, 6, 23, 30, 16)
    assert len(read_appointment_pages(tmp_path, max_pages=1)) == 1


def test_find_clinics_page_picks_the_newest_clinics_response(tmp_path):
    for folder, stamp in (("a", "2026-11-06T10:00:00+00:00"), ("b", "2026-11-06T12:00:00+00:00")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "02_clinics.json").write_text(json.dumps({"data": [{"id": folder}]}), encoding="utf-8")
        (tmp_path / folder / "02_clinics.meta.json").write_text(json.dumps({
            "path": "/v1/clinics", "params": {}, "status": 200, "body_saved_as": "02_clinics.json",
            "response_headers": {"date": "Fri, 06 Nov 2026 12:00:00 GMT"}, "fetched_at_utc": stamp,
        }), encoding="utf-8")
    page = find_clinics_page(tmp_path)
    assert page.endpoint == "clinics" and json.loads(page.body)["data"][0]["id"] == "b"


def test_normalisers_match_what_the_check_query_gives_back():
    assert split_scheduled("2026-10-01T08:00+02:00") == ("2026-10-01T08:00:00", 120)
    assert split_scheduled("2026-10-25T08:00+01:00") == ("2026-10-25T08:00:00", 60)
    assert split_scheduled("2026-10-01T08:00:30-05:30") == ("2026-10-01T08:00:30", -330)
    assert norm_utc("2026-09-30T10:34:11Z") == "2026-09-30T10:34:11"
    assert norm_utc(None) is None


def test_expected_appointments_latest_version_wins_and_ties_go_to_the_later_page():
    page = lambda rows: Page("appointments", {}, 1, datetime(2026, 11, 6), datetime(2026, 11, 6),
                             json.dumps({"data": rows}), len(rows))
    first = page([row(status="booked", updated="2026-10-01T06:00:00Z"), row("apt_000000000002")])
    second = page([row(status="completed", updated="2026-10-01T06:55:00Z")])
    third = page([row(status="no_show", updated="2026-10-01T06:55:00Z")])  # same stamp: later page wins
    assert expected_appointments([first, second])["apt_000000000001"][7] == "completed"
    assert expected_appointments([first, second, third])["apt_000000000001"][7] == "no_show"
    assert expected_appointments([second, first])["apt_000000000001"][7] == "completed"  # older stamp loses
    assert len(expected_appointments([first, second, third])) == 2


def test_expected_row_handles_optional_fields():
    cancelled = row(status="cancelled", cancelled_at="2026-10-01T05:00:07Z")
    page = Page("appointments", {}, 1, datetime(2026, 11, 6), datetime(2026, 11, 6),
                json.dumps({"data": [cancelled]}), 1)
    exp = expected_appointments([page])["apt_000000000001"]
    assert exp[-2:] == ("2026-10-01T05:00:07", None)
    assert exp[4:6] == ("2026-10-01T08:00:00", 120)


def test_compare_reports_each_kind_of_difference():
    exp = {"a": ("a", 1), "b": ("b", 2), "c": ("c", 3)}
    assert compare(exp, [("a", 1), ("b", 2), ("c", 3)]) == []
    problems = compare(exp, [("a", 1), ("b", 99), ("d", 4), ("d", 4)])
    text = " | ".join(problems)
    assert "duplicate ids" in text and "missing" in text and "do not have" in text and "1 rows differ" in text


def test_expected_clinics_keeps_non_ascii_names():
    page = Page("clinics", {}, 1, datetime(2026, 11, 6), datetime(2026, 11, 6), json.dumps({"data": [{
        "id": "CL-965", "name": "Nordvet København Ø", "city": "København Ø", "country": "DK",
        "timezone": "Europe/Copenhagen", "active": True, "opened_on": "2020-09-01"}]}, ensure_ascii=False), 0)
    assert expected_clinics(page)["CL-965"] == (
        "CL-965", "Nordvet København Ø", "København Ø", "DK", "Europe/Copenhagen", 1, "2020-09-01")


def test_a_replay_starts_at_the_pulls_earliest_page_not_at_the_clinics_response():
    # F1 (Phase 4): the run start must be the replayed pull's own time (D-035).
    def page(when):
        return Page("appointments", {}, 1, when, when, '{"data": []}')
    pages = [page(datetime(2026, 11, 6, 23, 27, 5)), page(datetime(2026, 11, 6, 23, 27, 3)),
             page(datetime(2026, 11, 6, 23, 29, 40))]
    assert pull_started_at(pages) == datetime(2026, 11, 6, 23, 27, 3)
