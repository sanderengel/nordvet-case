"""The fetcher against the fake API: windows, paging, incremental runs, 410 restarts."""

from datetime import date, datetime, timedelta

import pytest

from fake_api import FakeApi, appt
from nordvet.api.client import ApiError, HttpResponse
from nordvet.ingest import fetch
from nordvet.ingest.fetch import fetch_run, month_windows, updated_since_param


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


def now():
    return datetime(2026, 11, 7, 12, 0, 30)


def ids(result):
    import json

    out = []
    for p in result.pages:
        if p.endpoint == "appointments":
            out += [r["id"] for r in json.loads(p.body)["data"]]
    return out


def test_month_windows_cover_the_range_inclusively():
    w = month_windows(date(2026, 8, 1), date(2027, 1, 6))
    assert w[0] == (date(2026, 8, 1), date(2026, 8, 31))
    assert w[-1] == (date(2027, 1, 1), date(2027, 1, 31))
    assert len(w) == 6
    assert month_windows(date(2026, 8, 15), date(2026, 8, 20)) == [(date(2026, 8, 15), date(2026, 8, 31))]


def test_updated_since_is_the_run_start_minus_ten_minutes_with_a_z():
    assert updated_since_param(datetime(2026, 11, 7, 12, 0, 0)) == "2026-11-07T11:50:00Z"
    assert updated_since_param(datetime(2026, 11, 7, 0, 5, 0)) == "2026-11-06T23:55:00Z"


def test_full_run_reads_clinics_then_every_month_with_deleted_rows_included():
    api = FakeApi(dataset())
    result = fetch_run(api, now=now)

    assert api.calls[0] == ("/v1/clinics", {})
    appointment_calls = [p for path, p in api.calls if path == "/v1/appointments"]
    assert [c["from"] for c in appointment_calls] == [
        "2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01", "2026-12-01", "2027-01-01",
    ]
    assert all(c["include_deleted"] == "true" and c["limit"] == "500" for c in appointment_calls)
    assert all("updated_since" not in c and "cursor" not in c for c in appointment_calls)
    assert sorted(ids(result)) == sorted(r["id"] for r in dataset())  # the deleted row too
    assert result.windows == 6 and result.restarts == 0
    assert result.started_at == datetime(2026, 11, 7, 12, 0, 0)  # the clinics page's server Date


def test_pages_are_kept_as_received_numbered_per_month_and_without_cursors():
    api = FakeApi(dataset(), max_page=1)
    result = fetch_run(api, now=now)
    nov = [p for p in result.pages if p.endpoint == "appointments" and p.params["from"] == "2026-11-01"]
    assert [p.page_no for p in nov] == [1, 2, 3]  # three rows in November, one per page
    assert all("cursor" not in p.params for p in result.pages)
    assert [p.rows for p in nov] == [1, 1, 1]
    assert all(p.fetched_at == now() for p in result.pages)
    clinics = result.pages[0]
    assert clinics.endpoint == "clinics" and clinics.rows == 0 and "Århus" in clinics.body  # not escaped


def test_incremental_run_passes_updated_since_to_every_month():
    api = FakeApi(dataset())
    result = fetch_run(api, updated_since="2026-11-07T09:00:00Z", now=now)
    appointment_calls = [p for path, p in api.calls if path == "/v1/appointments"]
    assert all(c["updated_since"] == "2026-11-07T09:00:00Z" for c in appointment_calls)
    assert sorted(ids(result)) == ["apt_000000000004", "apt_000000000005"]  # updated_since is inclusive


def test_a_dead_cursor_restarts_that_month_without_duplicates_and_keeps_the_run_start():
    api = FakeApi(dataset(), max_page=1)
    api.expire_cursors = 1
    first_start = datetime(2026, 11, 7, 12, 0, 0)

    # time moves on during the run, as it would in reality
    original_get = api.get

    def moving(path, params=None):
        resp = original_get(path, params)
        api.now += timedelta(seconds=5)
        return resp

    api.get = moving
    result = fetch_run(api, now=now)
    assert result.restarts == 1
    assert sorted(ids(result)) == sorted(r["id"] for r in dataset())  # nothing twice, nothing lost
    assert result.started_at == first_start  # the restart did not move it


def test_a_month_that_keeps_expiring_stops_the_run():
    api = FakeApi(dataset(), max_page=1)
    api.expire_cursors = 99
    with pytest.raises(ApiError, match="expired 4 times"):
        fetch_run(api, now=now)


def test_a_repeated_cursor_stops_the_run():
    class Loop:
        def get(self, path, params=None):
            hdr = {"date": "Sat, 07 Nov 2026 12:00:00 GMT"}
            if path == "/v1/clinics":
                return HttpResponse(200, hdr, '{"data":[]}')
            return HttpResponse(200, hdr, '{"data":[],"next_cursor":"same"}')

    with pytest.raises(ApiError, match="repeated a cursor"):
        fetch_run(Loop(), now=now)


@pytest.mark.parametrize(
    "answer, message",
    [
        (HttpResponse(400, {"date": "Sat, 07 Nov 2026 12:00:00 GMT"}, '{"error":"bad"}'), "HTTP 400"),
        (HttpResponse(200, {"date": "Sat, 07 Nov 2026 12:00:00 GMT"}, "<html>"), "not JSON"),
        (HttpResponse(200, {"date": "Sat, 07 Nov 2026 12:00:00 GMT"}, '{"rows":[]}'), "no data list"),
        (HttpResponse(200, {}, '{"data":[],"next_cursor":null}'), "no Date header"),
    ],
)
def test_bad_answers_stop_the_run(answer, message):
    class Bad:
        def get(self, path, params=None):
            return answer

    with pytest.raises(ApiError, match=message):
        fetch_run(Bad(), now=now)
