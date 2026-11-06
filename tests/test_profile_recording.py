from collections import Counter
from datetime import date, datetime

from nordvet.explore.profile_recording import (
    confirmed_and_assumed,
    corr,
    day_counts,
    group_size_counts,
    to_local,
)


def test_to_local_converts_utc_to_copenhagen_time_across_dst():
    assert to_local("2026-10-01T06:55:00Z") == datetime(2026, 10, 1, 8, 55)  # summer time, UTC+2
    assert to_local("2026-11-02T07:00:00Z") == datetime(2026, 11, 2, 8, 0)  # winter time, UTC+1


def test_day_counts_keeps_only_days_before_the_cutoff_and_relevant_statuses():
    def row(day, status):
        return {"_local": datetime(2026, 11, day, 10, 0), "status": status}

    rows = [row(4, "completed"), row(4, "no_show"), row(4, "booked"), row(4, "cancelled"), row(5, "booked")]
    assert day_counts(rows, before=date(2026, 11, 5)) == {date(2026, 11, 4): (1, 1, 1)}


def test_confirmed_and_assumed_rates():
    assert confirmed_and_assumed(18, 0, 2) == (0.0, 0.1)  # the morning after: nothing confirmed yet
    assert confirmed_and_assumed(18, 2, 0) == (0.1, 0.1)  # settled day: both agree
    assert confirmed_and_assumed(0, 0, 0) == (0.0, 0.0)


def test_group_size_counts_and_corr():
    assert group_size_counts(["a", "a", "b"]) == Counter({2: 1, 1: 1})
    assert abs(corr([1, 2, 3], [2, 4, 6]) - 1.0) < 1e-9
    assert abs(corr([1, 2, 3], [3, 2, 1]) + 1.0) < 1e-9
