from collections import Counter
from datetime import datetime, timedelta, timezone

from nordvet.explore.profile_watermark import (
    Page,
    Snapshot,
    candidate_windows,
    classify_stamp,
    created_minus_updated,
    diff_rows,
    pair_findings,
    parse_http_date,
    parse_stamp,
    percentile,
    seconds_zero_share,
)

UTC = timezone.utc


def at(text: str) -> datetime:
    return parse_stamp(text)


def test_parse_http_date_and_stamp_are_utc_aware():
    assert parse_http_date("Fri, 06 Nov 2026 23:31:18 GMT") == datetime(2026, 11, 6, 23, 31, 18, tzinfo=UTC)
    assert parse_stamp("2026-11-06T23:31:18Z") == datetime(2026, 11, 6, 23, 31, 18, tzinfo=UTC)


def test_percentile_is_nearest_rank():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 50) == 5
    assert percentile(values, 95) == 10
    assert percentile([7], 99) == 7


def test_classify_stamp_against_a_read_interval():
    first, last = at("2026-11-06T17:30:00Z"), at("2026-11-06T17:31:00Z")
    assert classify_stamp(at("2026-11-06T17:31:01Z"), first, last, 60) == "after"
    assert classify_stamp(at("2026-11-06T17:30:30Z"), first, last, 60) == "during"
    assert classify_stamp(at("2026-11-06T17:29:30Z"), first, last, 60) == "just_before"
    assert classify_stamp(at("2026-11-06T17:28:59Z"), first, last, 60) == "before"


def test_candidate_windows_day_one_sits_in_two_windows():
    assert candidate_windows("2026-11-15T08:00+01:00") == ["2026-11"]
    assert candidate_windows("2026-11-01T08:00+01:00") == ["2026-11", "2026-10"]
    assert candidate_windows("2027-01-01T08:00+01:00") == ["2027-01", "2026-12"]


def test_diff_rows_finds_new_gone_and_changed():
    old = {"a": {"status": "booked"}, "b": {"status": "booked"}, "c": {"status": "booked"}}
    new = {"a": {"status": "completed"}, "b": {"status": "booked"}, "d": {"status": "booked"}}
    new_ids, gone_ids, changed = diff_rows(old, new)
    assert new_ids == ["d"] and gone_ids == ["c"]
    assert changed == [("a", {"status": "booked"}, {"status": "completed"})]


def test_seconds_zero_share_ignores_rows_without_the_field():
    rows = [{"updated_at": "2026-11-06T10:15:00Z"}, {"updated_at": "2026-11-06T10:15:07Z"}, {}]
    assert seconds_zero_share(rows, "updated_at") == (2, 0.5)
    assert seconds_zero_share(rows, "deleted_at") == (0, 0.0)


def test_created_minus_updated_buckets():
    def row(created, updated):
        return {"created_at": created, "updated_at": updated}

    rows = [
        row("2026-11-06T10:15:30Z", "2026-11-06T10:15:30Z"),  # equal
        row("2026-11-06T10:15:30Z", "2026-11-06T10:15:00Z"),  # updated_at cut down to the minute
        row("2026-11-06T10:15:00Z", "2026-11-06T10:15:20Z"),  # updated_at a little later
        row("2026-11-06T10:15:00Z", "2026-11-06T12:00:00Z"),  # touched later
    ]
    assert created_minus_updated(rows) == Counter({"equal": 1, "upd_floor": 1, "upd_later_<60s": 1, "apart": 1})


def snapshot(name: str, window: str, first: str, last: str, rows: list[dict]) -> Snapshot:
    """A snapshot with one window read in two pages, between `first` and `last` (server time)."""
    half = len(rows) // 2
    pages = [
        Page(window, at(first), at(first), 0.05, rows[:half]),
        Page(window, at(last), at(last), 0.05, rows[half:]),
    ]
    return Snapshot(name, {window}, {window: pages})


def appointment(rid: str, status: str, updated: str, created: str = "2026-11-01T08:00:00Z") -> dict:
    return {"id": rid, "status": status, "scheduled_at": "2026-11-05T09:00+01:00", "created_at": created, "updated_at": updated}


def test_pair_findings_separates_consistent_late_and_backwards_rows():
    earlier = snapshot(
        "earlier", "2026-11", "2026-11-06T17:30:00Z", "2026-11-06T17:31:00Z",
        [
            appointment("a", "booked", "2026-11-05T10:00:00Z"),
            appointment("b", "booked", "2026-11-05T10:00:00Z"),
            appointment("c", "booked", "2026-11-05T10:00:00Z"),
        ],
    )
    later = snapshot(
        "later", "2026-11", "2026-11-06T18:00:00Z", "2026-11-06T18:01:00Z",
        [
            appointment("a", "completed", "2026-11-06T17:45:00Z"),  # changed after the earlier read: consistent
            appointment("b", "completed", "2026-11-06T16:00:00Z"),  # stamp long before the read: visible late
            appointment("c", "booked", "2026-11-05T09:00:00Z"),  # updated_at went backwards
            appointment("d", "booked", "2026-11-06T17:50:10Z", created="2026-11-06T17:50:10Z"),  # new, consistent
            appointment("e", "booked", "2026-11-06T16:00:00Z", created="2026-11-06T16:00:00Z"),  # new, should have been visible
        ],
    )
    f = pair_findings(earlier, later, ["2026-11"], margin=60)
    assert (f["new"], f["gone"], f["changed"]) == (2, 0, 3)
    assert f["new_created"] == Counter({"after": 1, "before": 1})
    assert f["changed_updated"] == Counter({"after": 1, "before": 2})
    assert f["backwards"] == 1
    assert f["transitions"] == Counter({("booked", "completed"): 2})
    assert f["after_gap_min"] == 14 * 60  # 17:45:00 against the end of the read at 17:31:00
    assert len(f["examples"]) >= 2


def test_pair_findings_reports_rows_that_vanish():
    earlier = snapshot("earlier", "2026-11", "2026-11-06T17:30:00Z", "2026-11-06T17:31:00Z",
                       [appointment("a", "booked", "2026-11-05T10:00:00Z"), appointment("b", "booked", "2026-11-05T10:00:00Z")])
    later = snapshot("later", "2026-11", "2026-11-06T18:00:00Z", "2026-11-06T18:01:00Z",
                     [appointment("a", "booked", "2026-11-05T10:00:00Z")])
    f = pair_findings(earlier, later, ["2026-11"], margin=60)
    assert f["gone"] == 1 and f["new"] == 0 and f["changed"] == 0
