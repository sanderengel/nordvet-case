"""Tests for the independent clinic-day check (its own rules, written again in Python). No database."""

from datetime import date, datetime
from decimal import Decimal

from nordvet.ingest.raw import Page
from nordvet.model.sample_check import (
    COUNTS,
    as_of_dates,
    cancelled_after_start,
    clinic_days,
    compare,
    latest_rows,
    outcome,
)


def row(id_, scheduled, status, updated="2026-11-05T10:00:00Z", clinic="CL-001", **extra):
    r = {"id": id_, "clinic_id": clinic, "scheduled_at": scheduled, "status": status, "updated_at": updated}
    r.update(extra)
    return r


def page(rows):
    import json
    when = datetime(2026, 11, 7, 12, 0, 0)
    return Page("appointments", {}, 1, when, when, json.dumps({"data": rows}))


def test_latest_version_wins_and_a_tie_goes_to_the_later_page():
    old = row("a", "2026-11-05T09:00+01:00", "booked", updated="2026-11-04T08:00:00Z")
    new = row("a", "2026-11-05T09:00+01:00", "no_show", updated="2026-11-06T08:00:00Z")
    tie = row("a", "2026-11-05T09:00+01:00", "completed", updated="2026-11-06T08:00:00Z")
    assert latest_rows([page([new]), page([old])])[0]["status"] == "no_show"
    assert latest_rows([page([new]), page([tie])])[0]["status"] == "completed"


def test_as_of_date_follows_each_clinics_zone():
    # 23:30 UTC on 6 Nov is 00:30 on 7 Nov in Copenhagen and Stockholm, and still 6 Nov in London
    got = as_of_dates(datetime(2026, 11, 6, 23, 30), {"DK": "Europe/Copenhagen", "SE": "Europe/Stockholm",
                                                      "UK": "Europe/London"})
    assert got == {"DK": date(2026, 11, 7), "SE": date(2026, 11, 7), "UK": date(2026, 11, 6)}


def test_outcomes():
    as_of = date(2026, 11, 7)
    assert outcome(row("a", "2026-11-06T09:00+01:00", "booked"), as_of) == "no_show_assumed"
    assert outcome(row("a", "2026-11-07T09:00+01:00", "booked"), as_of) == "pending"
    assert outcome(row("a", "2026-11-07T09:00+01:00", "completed"), as_of) == "completed"  # D-043
    assert outcome(row("a", "2026-11-20T09:00+01:00", "cancelled"), as_of) == "cancelled"
    assert outcome(row("a", "2026-11-06T09:00+01:00", "rescheduled"), as_of) == "other"


def test_cancelled_after_start_compares_instants_not_wall_times():
    start = "2026-11-05T09:00+01:00"  # 08:00 UTC
    assert cancelled_after_start(row("a", start, "cancelled", cancelled_at="2026-11-05T08:00:00Z"))  # at the start
    assert not cancelled_after_start(row("a", start, "cancelled", cancelled_at="2026-11-05T07:59:00Z"))
    assert cancelled_after_start(row("a", start, "cancelled", cancelled_at="2026-11-05T08:30:00Z"))
    assert not cancelled_after_start(row("a", start, "completed"))


def test_clinic_days_counts_rates_and_the_rules_for_no_rate():
    rows = [
        row("1", "2026-11-05T09:00+01:00", "completed"),
        row("2", "2026-11-05T09:30+01:00", "no_show"),
        row("3", "2026-11-05T10:00+01:00", "booked"),
        row("4", "2026-11-05T10:30+01:00", "booked"),
        row("5", "2026-11-05T11:00+01:00", "cancelled", cancelled_at="2026-11-05T10:30:00Z"),
        row("6", "2026-11-05T12:00+01:00", "no_show", deleted_at="2026-11-06T09:00:00Z"),  # D-015
        row("7", "2026-11-07T08:00+01:00", "completed"),
        row("8", "2026-11-08T08:00+01:00", "cancelled", cancelled_at="2026-11-01T08:00:00Z"),
    ]
    days = clinic_days(rows, {"CL-001": date(2026, 11, 7)})
    thu = days[("CL-001", date(2026, 11, 5))]
    assert thu.counts == {"appointments": 5, "completed": 1, "no_show": 1, "no_show_assumed": 2,
                          "cancelled": 1, "cancelled_after_start": 1, "pending": 0}
    assert thu.no_show_rate == 0.75 and thu.confirmed_no_show_rate == 0.5
    sat = days[("CL-001", date(2026, 11, 7))]
    assert sat.counts["completed"] == 1 and sat.no_show_rate is None  # no rate on the as-of day
    sun = days[("CL-001", date(2026, 11, 8))]
    assert sun.counts["cancelled"] == 1 and sun.no_show_rate is None


def test_compare_finds_count_rate_and_missing_differences():
    days = clinic_days([row("1", "2026-11-05T09:00+01:00", "completed"),
                        row("2", "2026-11-05T09:30+01:00", "no_show")], {"CL-001": date(2026, 11, 7)})
    good = ("CL-001", date(2026, 11, 5), 2, 1, 1, 0, 0, 0, 0, Decimal("0.500000"), Decimal("0.500000"))
    assert len(good) == 2 + len(COUNTS) + 2
    assert compare(days, [good]) == []
    assert compare(days, [good[:-2] + (Decimal("0.400000"), Decimal("0.500000"))])  # a rate differs
    assert compare(days, [good[:3] + (2,) + good[4:]])  # a count differs
    assert compare(days, []) == ["missing in the mart: CL-001 2026-11-05"]
    extra = ("CL-002",) + good[1:]
    assert compare(days, [good, extra]) == ["only in the mart: CL-002 2026-11-05"]
