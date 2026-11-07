"""Independent check of the marts (Phase 4, D-038): recompute every clinic-day in Python straight
from a saved pull's JSON and compare with marts.clinic_day.

    MSSQL_DATABASE=nordvet_p3 python -m nordvet.model.sample_check --pull data/raw/20261106T174032Z_pull

The database must hold that pull's replay and the built marts (see sql/checks/regression_p3.sql).
Nothing here uses the SQL of the model: the latest version per id, the soft-delete rule (D-015),
the as-of date per clinic zone (D-035, D-042, with Python's zoneinfo instead of SQL Server's
Windows names), the outcomes (D-021, D-043), cancelled_after_start (D-022) and both rates
(D-020, D-021) are written again here. Only the reading of the saved files is shared with the replay.

D-038 asked for a random sample of clinic-days; computing all of them costs nothing more, so all
are compared, and the report also prints a seeded random sample of rows for the eye.

Exit codes: 0 every clinic-day matches, 1 configuration, 2 a difference or the check failed.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nordvet.config import ConfigError, load_settings, redact
from nordvet.db.connection import connect
from nordvet.ingest.raw import Page
from nordvet.ingest.replay import DEFAULT_START, find_clinics_page, pull_started_at, read_appointment_pages

EXIT_OK, EXIT_CONFIG, EXIT_FAILED = 0, 1, 2
COUNTS = ("appointments", "completed", "no_show", "no_show_assumed", "cancelled", "cancelled_after_start", "pending")


def parse_ts(value: str) -> datetime:
    """'2026-10-01T08:00+02:00' or '2026-10-01T06:55:00Z' -> an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def latest_rows(pages: list[Page]) -> list[dict]:
    """One row per id: the highest updated_at; on a tie the row from the later page."""
    latest: dict[str, dict] = {}
    for page in pages:
        for row in json.loads(page.body)["data"]:
            current = latest.get(row["id"])
            if current is None or row["updated_at"] >= current["updated_at"]:
                latest[row["id"]] = row
    return list(latest.values())


def as_of_dates(run_start_utc: datetime, timezones: dict[str, str]) -> dict[str, date]:
    """Each clinic's local date at the run start (naive UTC in, as raw.run holds it)."""
    aware = run_start_utc.replace(tzinfo=timezone.utc)
    return {cid: aware.astimezone(ZoneInfo(tz)).date() for cid, tz in timezones.items()}


def outcome(row: dict, as_of: date) -> str:
    status = row["status"]
    if status in ("completed", "no_show", "cancelled"):
        return status
    if status == "booked":
        return "no_show_assumed" if date.fromisoformat(row["scheduled_at"][:10]) < as_of else "pending"
    return "other"


def cancelled_after_start(row: dict) -> bool:
    return (row["status"] == "cancelled" and row.get("cancelled_at") is not None
            and parse_ts(row["cancelled_at"]) >= parse_ts(row["scheduled_at"]))


@dataclass
class Day:
    counts: dict = field(default_factory=lambda: dict.fromkeys(COUNTS, 0))
    no_show_rate: float | None = None
    confirmed_no_show_rate: float | None = None


def clinic_days(rows: list[dict], as_of: dict[str, date]) -> dict[tuple[str, date], Day]:
    """Counts and rates per (clinic, local date), for every clinic-day that has an appointment.
    Soft-deleted rows are left out; rates only for days before the clinic's as-of date, and
    only when there is something to divide."""
    days: dict[tuple[str, date], Day] = defaultdict(Day)
    for row in rows:
        if row.get("deleted_at"):
            continue
        d = date.fromisoformat(row["scheduled_at"][:10])  # the API's local date (Q1)
        c = days[(row["clinic_id"], d)].counts
        c["appointments"] += 1
        o = outcome(row, as_of[row["clinic_id"]])
        if o in c:
            c[o] += 1
        c["cancelled_after_start"] += cancelled_after_start(row)
    for (cid, d), day in days.items():
        if d >= as_of[cid]:
            continue
        c = day.counts
        attended_or_missed = c["completed"] + c["no_show"] + c["no_show_assumed"]
        if attended_or_missed:
            day.no_show_rate = (c["no_show"] + c["no_show_assumed"]) / attended_or_missed
        if c["completed"] + c["no_show"]:
            day.confirmed_no_show_rate = c["no_show"] / (c["completed"] + c["no_show"])
    return dict(days)


def _same_rate(python: float | None, mart) -> bool:
    if python is None or mart is None:
        return python is None and mart is None
    return abs(round(python, 6) - float(mart)) < 1e-9


def compare(expected: dict[tuple[str, date], Day], mart_rows: list[tuple]) -> list[str]:
    """mart_rows: (clinic_id, date, *COUNTS, no_show_rate, confirmed_no_show_rate) for clinic-days
    with at least one appointment. Returns one line per difference."""
    actual = {(r[0], r[1]): r for r in mart_rows}
    problems = []
    for key in sorted(expected.keys() - actual.keys())[:10]:
        problems.append(f"missing in the mart: {key[0]} {key[1]}")
    for key in sorted(actual.keys() - expected.keys())[:10]:
        problems.append(f"only in the mart: {key[0]} {key[1]}")
    for key in sorted(expected.keys() & actual.keys()):
        day, row = expected[key], actual[key]
        mart_counts = dict(zip(COUNTS, row[2:2 + len(COUNTS)]))
        if mart_counts != day.counts:
            problems.append(f"{key[0]} {key[1]}: counts python {day.counts} mart {mart_counts}")
        elif not _same_rate(day.no_show_rate, row[-2]) or not _same_rate(day.confirmed_no_show_rate, row[-1]):
            problems.append(f"{key[0]} {key[1]}: rates python {day.no_show_rate}, {day.confirmed_no_show_rate} "
                            f"mart {row[-2]}, {row[-1]}")
    return problems


MART_QUERY = (
    "SELECT clinic_id, calendar_date, " + ", ".join(COUNTS) + ", no_show_rate, confirmed_no_show_rate "
    "FROM marts.clinic_day WHERE appointments > 0"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet.model.sample_check", description=__doc__.split("\n\n")[0])
    parser.add_argument("--pull", required=True, type=Path, help="the saved pull the database was replayed from")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"), help="where saved responses live")
    parser.add_argument("--start", default=DEFAULT_START, help="first window start, as for the replay")
    parser.add_argument("--show", type=int, default=8, help="random clinic-days to print side by side")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return EXIT_CONFIG
    if not settings.mssql_password:
        print("Missing in the env file: MSSQL_SA_PASSWORD")
        return EXIT_CONFIG

    pages = read_appointment_pages(args.pull, args.start)
    if not pages:
        print(f"no all-rows appointment pages in {args.pull}")
        return EXIT_FAILED
    clinics = json.loads(find_clinics_page(args.raw_dir).body)["data"]
    run_start = pull_started_at(pages)
    as_of = as_of_dates(run_start, {c["id"]: c["timezone"] for c in clinics})
    expected = clinic_days(latest_rows(pages), as_of)
    print(f"python: {len(expected)} clinic-days from {args.pull.name}, run start {run_start} UTC, "
          f"as-of dates {sorted(set(as_of.values()))}")

    try:
        conn = connect(settings)
        try:
            cur = conn.cursor()
            cur.execute(MART_QUERY)
            mart_rows = [tuple(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        print(f"Check failed to run: {type(exc).__name__}: {redact(exc, settings)}")
        return EXIT_FAILED
    print(f"mart:   {len(mart_rows)} clinic-days with appointments in {settings.mssql_database}.marts.clinic_day")

    shown = random.Random(args.seed).sample(sorted(expected), min(args.show, len(expected)))
    actual = {(r[0], r[1]): r for r in mart_rows}
    for key in sorted(shown):
        day, row = expected[key], actual.get(key)
        print(f"  {key[0]} {key[1]}  python {tuple(day.counts.values())} "
              f"{day.no_show_rate if day.no_show_rate is None else round(day.no_show_rate, 4)}  "
              f"mart {row[2:2 + len(COUNTS)] if row else None} {row[-2] if row else None}")

    problems = compare(expected, mart_rows)
    for line in problems[:20]:
        print(f"  DIFF {line}")
    if len(problems) > 20:
        print(f"  ... and {len(problems) - 20} more")
    if problems:
        print(f"\nRESULT: FAIL ({len(problems)} differences)")
    else:
        print(f"\nRESULT: PASS (all {len(expected)} clinic-days identical)")
    return EXIT_OK if not problems else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
