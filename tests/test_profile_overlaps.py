from collections import Counter
from datetime import datetime, timedelta

from nordvet.explore.profile_overlaps import (
    expected_pairs,
    is_taken,
    minutes_at_level,
    overlap_flags,
    peak_concurrency,
)

T0 = datetime(2026, 10, 1, 8, 0)


def appt(rid, start_min, length, created_min=-600, cancelled_min=None):
    return {
        "id": rid,
        "_s": T0 + timedelta(minutes=start_min),
        "_e": T0 + timedelta(minutes=start_min + length),
        "_c": T0 + timedelta(minutes=created_min),
        "_x": None if cancelled_min is None else T0 + timedelta(minutes=cancelled_min),
    }


def test_peak_concurrency_counts_the_busiest_moment_and_treats_touching_as_no_overlap():
    group = [appt("a", 0, 60), appt("b", 15, 15), appt("c", 20, 15), appt("d", 60, 15)]
    peak = peak_concurrency(group)
    assert peak == {"a": 3, "b": 3, "c": 3, "d": 1}  # d starts when a ends


def test_minutes_at_level_splits_booked_time_by_concurrency():
    group = [appt("a", 0, 30), appt("b", 15, 30)]
    assert minutes_at_level(group) == Counter({1: 30, 2: 15})


def test_is_taken_ignores_later_bookings_and_earlier_cancellations():
    booked_before = appt("a", 0, 30)
    booked_after = appt("b", 0, 30, created_min=-10)
    cancelled_before = appt("c", 0, 30, cancelled_min=-30)
    at = T0 - timedelta(minutes=20)
    start, end = T0 + timedelta(minutes=10), T0 + timedelta(minutes=20)
    assert is_taken([booked_before], start, end, at, exclude="x")
    assert not is_taken([booked_after, cancelled_before], start, end, at, exclude="x")
    assert not is_taken([booked_before], start, end, at, exclude="a")


def test_overlap_flags_marks_both_sides_only():
    groups = {"v": [appt("a", 0, 30), appt("b", 20, 10), appt("c", 30, 10)]}
    assert overlap_flags(groups) == {"a": True, "b": True, "c": False}


def test_expected_pairs_weights_by_duration_and_sums_to_total():
    exp = expected_pairs(Counter({"short": 2, "long": 2}), {"short": 10, "long": 30}, total_pairs=100)
    assert abs(sum(exp.values()) - 100) < 1e-9
    assert exp[("long", "long")] > exp[("short", "short")]


def test_observed_vs_expected_adjusts_for_strata():
    from nordvet.explore.profile_overlaps import observed_vs_expected

    # Stratum "x" always 1, stratum "y" always 0. Bucket 2 is all "x", so its high mean is fully expected.
    records = [("x", 2, 1), ("x", 2, 1), ("y", 1, 0), ("y", 1, 0), ("x", 1, 1)]
    result = observed_vs_expected(records)
    assert result[2] == (2, 1.0, 1.0)
    n, observed, expected = result[1]
    assert n == 3 and abs(observed - 1 / 3) < 1e-9 and abs(expected - 1 / 3) < 1e-9
