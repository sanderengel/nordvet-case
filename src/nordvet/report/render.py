"""The no-show report for the Head of Operations (Phase 5, D-047 to D-054).

    python -m nordvet.report.render [--out reports/no-shows.html] [-v]

Reads marts.report_clinic_day and marts.report_all_clinics_day (D-047) and writes one
self-contained HTML file: one table for one day (D-050), with one row per clinic that day (every
open clinic, and a closed clinic on the days it had appointments, D-055) under a pinned "All
row "All" (D-056). The page opens on "yesterday" (the as-of date minus
one day) or, when that day had no appointments (a Sunday), on the latest day before it that had
some (D-052). A dropdown shows any earlier day; every day's rows are embedded in the page, so
the file needs no server and no other files.

Every number on the page comes from the view. Python only sorts, formats and lays out:
  - clinic names are shown without the "Nordvet " every name starts with (Sander, Phase 5);
  - rows are embedded in the default order: the day's no-show rate, highest first, rows without
    a rate last; ties by the number missed (no-shows + not yet recorded), then by clinic name
    (D-050). On the page any column can be sorted; the script sorts a copy of the rows in this
    order with a stable sort, so ties in any column fall back to the default order, and rows
    without a rate stay last in both directions. The page shows the first 15 rows of whatever
    order is chosen, with a button to show every clinic. The "All" row stays on top and is never
    sorted or counted in the 15;
  - a rate is followed by "assumed" while the day still has appointments not yet recorded (D-051);
  - the 30-day column covers the 30 days before the day shown, not the day itself, so it is a
    normal to compare the day against. It is headed "Previous N days", N being the days of data
    its window covers: 30, or fewer near the first day with any appointment (the view's days_30d,
    the same for every row of a day); "No earlier days" on that first day;
  - the 30-day rate carries no mark (D-053): on the latest days it holds a few not-yet-recorded
    appointments among several hundred, and the note under the table says it is counted the same way.

Build first (python -m nordvet.model.build). The file is written to a temporary name and then
renamed, so a failed run leaves the previous report in place.

Exit codes (D-033): 0 ok, 1 configuration problem, 2 the report failed.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from nordvet.config import ConfigError, load_settings, redact
from nordvet.db.connection import connect, ensure_database_with_retry

log = logging.getLogger("nordvet.report")

DEFAULT_OUT = Path(__file__).resolve().parents[3] / "reports" / "no-shows.html"
TEMPLATE = Path(__file__).resolve().parent / "template.html"
EXIT_OK, EXIT_CONFIG, EXIT_FAILED = 0, 1, 2
NAME_PREFIX = "Nordvet "

QUERY = """
SELECT clinic_id, clinic_name, country, calendar_date,
       completed, no_show, no_show_assumed, attended_or_missed,
       no_show_rate, CAST(is_assumed AS INT), no_show_rate_30d, days_30d,
       as_of_date, CAST(as_of_local AS DATETIME2(0))
FROM marts.report_clinic_day
ORDER BY calendar_date, clinic_id
"""

TOTALS_QUERY = """
SELECT calendar_date, completed, no_show, no_show_assumed, attended_or_missed,
       no_show_rate, CAST(is_assumed AS INT), no_show_rate_30d, days_30d
FROM marts.report_all_clinics_day
ORDER BY calendar_date
"""
TOTAL_LABEL = "All"

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


class ReportError(RuntimeError):
    """The data cannot be shown as one report (no rows, or clinics as of different dates)."""


@dataclass(frozen=True)
class Row:
    """One clinic on one past day, as marts.report_clinic_day gives it."""

    clinic_id: str
    clinic_name: str
    country: str
    calendar_date: date
    completed: int
    no_show: int
    no_show_assumed: int
    attended_or_missed: int
    no_show_rate: Decimal | None
    is_assumed: bool
    no_show_rate_30d: Decimal | None
    days_30d: int
    as_of_date: date
    as_of_local: datetime

    @property
    def missed(self) -> int:
        return self.no_show + self.no_show_assumed


@dataclass(frozen=True)
class Total:
    """Every clinic summed on one past day, as marts.report_all_clinics_day gives it."""

    calendar_date: date
    completed: int
    no_show: int
    no_show_assumed: int
    attended_or_missed: int
    no_show_rate: Decimal | None
    is_assumed: bool
    no_show_rate_30d: Decimal | None
    days_30d: int


def fetch(conn) -> list[Row]:
    cur = conn.cursor()
    cur.execute(QUERY)
    return [
        Row(str(r[0]), str(r[1]), str(r[2]), r[3], int(r[4]), int(r[5]), int(r[6]), int(r[7]),
            r[8], bool(r[9]), r[10], int(r[11]), r[12], r[13])
        for r in cur.fetchall()
    ]


def fetch_totals(conn) -> list[Total]:
    cur = conn.cursor()
    cur.execute(TOTALS_QUERY)
    return [Total(r[0], int(r[1]), int(r[2]), int(r[3]), int(r[4]), r[5], bool(r[6]), r[7], int(r[8]))
            for r in cur.fetchall()]


def long_date(d: date) -> str:
    """'Friday 6 November 2026', independent of the machine's locale."""
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def short_date(d: date) -> str:
    """'Fri 6 Nov 2026', for the dropdown."""
    return f"{WEEKDAYS[d.weekday()][:3]} {d.day} {MONTHS[d.month - 1][:3]} {d.year}"


def display_name(name: str) -> str:
    """The clinic name without the "Nordvet " prefix every clinic shares."""
    return name[len(NAME_PREFIX):] if name.startswith(NAME_PREFIX) and len(name) > len(NAME_PREFIX) else name


def sort_value(rate: Decimal | None) -> float | None:
    """The rate as a plain number for sorting on the page (the text shown is format_rate)."""
    return None if rate is None else float(rate)


def window_label(days: int) -> str:
    """The 30-day column's header: 'Previous 30 days', or fewer days where the data is shorter."""
    if days == 0:
        return "No earlier days"
    return "Previous day" if days == 1 else f"Previous {days} days"


def format_rate(rate: Decimal | None) -> str:
    """'21.7%' (rounded half up to one decimal), or 'N/A' when there is nothing to divide."""
    if rate is None:
        return "N/A"
    pct = (Decimal(rate) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{pct}%"


def sort_rows(rows: list[Row]) -> list[Row]:
    """Highest rate first; no rate last; ties by number missed, then clinic name (D-050)."""
    return sorted(rows, key=lambda r: (r.no_show_rate is None, -(r.no_show_rate or 0), -r.missed, r.clinic_name))


def as_of(rows: list[Row]) -> tuple[date, datetime]:
    """The one as-of date and local time of the report.

    Every clinic is as of the same run, and Danish and Swedish clinics share one local date and
    time (D-042). A clinic in another time zone could have a different one; the page cannot show
    one "yesterday" for both, so the report fails and says so rather than mixing days.
    """
    if not rows:
        raise ReportError("marts.report_clinic_day is empty: load and build first")
    stamps = {(r.as_of_date, r.as_of_local) for r in rows}
    if len(stamps) != 1:
        raise ReportError(
            f"the clinics are as of {len(stamps)} different local dates or times "
            "(a clinic in another time zone?); the report shows one day for every clinic"
        )
    return stamps.pop()


def table_row(name, country, completed, no_show, assumed, total, rate, is_assumed, rate_30d) -> list:
    """[clinic, country, attended, no-show, not yet recorded, total, rate text, assumed,
    30-day rate text, rate, 30-day rate]; the last two are for sorting on the page."""
    return [name, country, completed, no_show, assumed, total, format_rate(rate),
            is_assumed and rate is not None, format_rate(rate_30d), sort_value(rate), sort_value(rate_30d)]


def page_data(rows: list[Row], totals: list[Total]) -> dict:
    """Everything the page shows, as plain strings and numbers, with the newest day first.

    Only days on which at least one clinic had an attended-or-missed appointment are offered
    (Sundays have none). The default day is the newest of them. Each day carries its clinic rows
    in the default order and its "All" row.
    """
    as_of_date, as_of_local = as_of(rows)
    total_by_day = {t.calendar_date: t for t in totals}
    by_day: dict[date, list[Row]] = defaultdict(list)
    for r in rows:
        by_day[r.calendar_date].append(r)
    days = sorted((d for d, rs in by_day.items() if any(r.attended_or_missed for r in rs)), reverse=True)
    if not days:
        raise ReportError("no past day has any attended or missed appointment")
    missing = [d for d in days if d not in total_by_day]
    if missing:
        raise ReportError(f"marts.report_all_clinics_day has no row for {missing[0].isoformat()}")

    yesterday = as_of_date - timedelta(days=1)
    default = days[0]
    note = ""
    if default != yesterday:
        note = f"{long_date(yesterday)} had no appointments; this is the last day that did."

    return {
        "asOf": f"{long_date(as_of_local.date())}, {as_of_local:%H:%M}",
        "defaultNote": note,
        "days": [
            {
                "date": d.isoformat(),
                "label": short_date(d),
                "title": long_date(d),
                "windowLabel": window_label(t.days_30d),
                "total": table_row(TOTAL_LABEL, "", t.completed, t.no_show, t.no_show_assumed,
                                   t.attended_or_missed, t.no_show_rate, t.is_assumed, t.no_show_rate_30d),
                "rows": [
                    table_row(display_name(r.clinic_name), r.country, r.completed, r.no_show,
                              r.no_show_assumed, r.attended_or_missed, r.no_show_rate, r.is_assumed,
                              r.no_show_rate_30d)
                    for r in sort_rows(by_day[d])
                ],
            }
            for d in days
            for t in [total_by_day[d]]
        ],
    }


def render(data: dict, template: Path = TEMPLATE) -> str:
    """The HTML page: the template with the data embedded as JSON.

    The default day is written in as text too, so the page names its date even before the
    script has run. '</' is escaped so no clinic name can end the script element.
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    first = data["days"][0]
    return (
        template.read_text(encoding="utf-8")
        .replace("{{TITLE}}", html.escape(first["title"]))
        .replace("{{AS_OF}}", html.escape(data["asOf"]))
        .replace("{{DATA}}", payload)
    )


def write(text: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)


def report(conn, out: Path = DEFAULT_OUT) -> dict:
    """Read the view, write the page. Returns the page data."""
    data = page_data(fetch(conn), fetch_totals(conn))
    write(render(data), out)
    return data


def _reachable(settings) -> bool:
    connect(settings).close()
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet.report.render", description="Write the no-show report")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="the HTML file (default: reports/no-shows.html)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging, with tracebacks")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return EXIT_CONFIG
    if not settings.mssql_password:
        log.error("missing in the env file: MSSQL_SA_PASSWORD")
        return EXIT_CONFIG

    conn = None
    try:
        ensure_database_with_retry(settings, ensure=_reachable)
        conn = connect(settings)
        data = report(conn, args.out)
    except Exception as exc:
        log.error("report failed: %s: %s", type(exc).__name__, redact(exc, settings))
        log.debug("traceback", exc_info=True)
        return EXIT_FAILED
    finally:
        if conn is not None:
            conn.close()
    first = data["days"][0]
    log.info("wrote %s: %s, %d clinics, %d days to choose from; data as of %s",
             args.out, first["title"], len(first["rows"]), len(data["days"]), data["asOf"])
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
