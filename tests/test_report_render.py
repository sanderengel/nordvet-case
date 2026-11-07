"""Tests for the no-show report that need no database (Phase 5, D-050 to D-053): sorting, formatting,
the default day, the as-of rule and the HTML itself."""

import json
import re
from datetime import date, datetime
from decimal import Decimal

import pytest

from nordvet.report.render import (
    ReportError,
    Row,
    Total,
    as_of,
    display_name,
    format_rate,
    long_date,
    page_data,
    render,
    short_date,
    sort_rows,
    window_label,
    write,
)

AS_OF_DATE = date(2026, 11, 7)  # a Saturday
AS_OF_LOCAL = datetime(2026, 11, 7, 6, 30)


def row(name, day=date(2026, 11, 6), completed=10, no_show=1, assumed=0, rate="auto", rate_30d=Decimal("0.1"),
        as_of_date=AS_OF_DATE, as_of_local=AS_OF_LOCAL, country="DK", days_30d=30):
    total = completed + no_show + assumed
    if rate == "auto":
        rate = (Decimal(no_show + assumed) / total).quantize(Decimal("0.000001")) if total else None
    return Row(f"CL-{name}", name, country, day, completed, no_show, assumed, total, rate, assumed > 0,
               rate_30d, days_30d, as_of_date, as_of_local)


def totals(rows):
    """The "All" rows for these rows: the day's counts summed (30-day rate fixed at 10%)."""
    by_day = {}
    for r in rows:
        by_day.setdefault(r.calendar_date, []).append(r)
    out = []
    for d, rs in by_day.items():
        c, n, s = (sum(r.completed for r in rs), sum(r.no_show for r in rs), sum(r.no_show_assumed for r in rs))
        t = c + n + s
        out.append(Total(d, c, n, s, t, (Decimal(n + s) / t).quantize(Decimal("0.000001")) if t else None,
                         s > 0, Decimal("0.1"), rs[0].days_30d))
    return out


def page(rows):
    return page_data(rows, totals(rows))


def test_rates_are_percentages_with_one_decimal_or_na():
    assert format_rate(Decimal("0.217391")) == "21.7%"
    assert format_rate(Decimal("0.000000")) == "0.0%"
    assert format_rate(Decimal("0.062500")) == "6.3%"  # half up, not to even
    assert format_rate(Decimal("1.000000")) == "100.0%"
    assert format_rate(None) == "N/A"  # nothing to divide (D-039)


def test_clinic_names_lose_the_shared_prefix():
    assert display_name("Nordvet Hillerød") == "Hillerød"
    assert display_name("Nordvet København V") == "København V"
    assert display_name("Hillerød") == "Hillerød"   # no prefix: unchanged
    assert display_name("Nordvet ") == "Nordvet "   # never an empty name


def test_dates_are_written_in_english_whatever_the_locale():
    assert long_date(date(2026, 11, 6)) == "Friday 6 November 2026"
    assert short_date(date(2026, 11, 9)) == "Mon 9 Nov 2026"


def test_rows_sort_by_rate_then_missed_then_name_with_no_rate_last():
    rows = [
        row("Empty", completed=0, no_show=0),          # no rate
        row("Low", completed=19, no_show=1),           # 5%
        row("Small", completed=9, no_show=1),          # 10%, 1 missed
        row("Big", completed=18, no_show=2),           # 10%, 2 missed
        row("Assumed", completed=8, assumed=2, no_show=0),  # 20%
        row("Bravo", completed=19, no_show=1),         # 5%, same as Low: by name
    ]
    assert [r.clinic_name for r in sort_rows(rows)] == ["Assumed", "Big", "Small", "Bravo", "Low", "Empty"]


def test_one_as_of_for_every_clinic_or_the_report_fails():
    assert as_of([row("A"), row("B")]) == (AS_OF_DATE, AS_OF_LOCAL)
    with pytest.raises(ReportError, match="different"):
        as_of([row("A"), row("B", as_of_date=date(2026, 11, 8))])
    with pytest.raises(ReportError, match="empty"):
        as_of([])


def test_the_page_opens_on_yesterday_and_lists_the_days_newest_first():
    rows = [row("A"), row("B", completed=5, no_show=0, assumed=1),
            row("A", day=date(2026, 11, 5)), row("B", day=date(2026, 11, 5))]
    data = page(rows)
    assert [d["date"] for d in data["days"]] == ["2026-11-06", "2026-11-05"]
    assert data["days"][0]["title"] == "Friday 6 November 2026"
    assert data["defaultNote"] == ""
    assert data["asOf"] == "Saturday 7 November 2026, 06:30"
    # [clinic, country, attended, no-show, not yet recorded, total, rate text, assumed,
    #  30-day rate text, rate, 30-day rate], in the default order
    assert data["days"][0]["rows"] == [
        ["B", "DK", 5, 0, 1, 6, "16.7%", True, "10.0%", 0.166667, 0.1],
        ["A", "DK", 10, 1, 0, 11, "9.1%", False, "10.0%", 0.090909, 0.1],
    ]


def test_a_day_without_appointments_is_skipped_and_the_page_says_so():
    # As of Monday 9 Nov: yesterday is Sunday 8 Nov, with nothing to show at any clinic (D-052).
    monday = date(2026, 11, 9)
    stamp = dict(as_of_date=monday, as_of_local=datetime(2026, 11, 9, 6, 30))
    rows = [row("A", day=date(2026, 11, 8), completed=0, no_show=0, **stamp),
            row("B", day=date(2026, 11, 8), completed=0, no_show=0, **stamp),
            row("A", day=date(2026, 11, 7), **stamp),
            row("B", day=date(2026, 11, 7), completed=0, no_show=0, **stamp)]
    data = page(rows)
    assert [d["date"] for d in data["days"]] == ["2026-11-07"]
    assert data["defaultNote"] == "Sunday 8 November 2026 had no appointments; this is the last day that did."
    # a clinic with nothing that day: N/A, last, and no number to sort on
    assert [(r[6], r[9]) for r in data["days"][0]["rows"]] == [("9.1%", 0.090909), ("N/A", None)]


def test_no_rate_means_no_assumed_mark():
    data = page([row("A"), row("B", completed=0, no_show=0, rate=None)])
    assert [r[7] for r in data["days"][0]["rows"]] == [False, False]


def test_no_day_with_appointments_fails():
    with pytest.raises(ReportError, match="no past day"):
        page([row("A", completed=0, no_show=0)])


def test_the_html_holds_the_data_and_names_the_day_without_running_the_script(tmp_path):
    data = page([row("Nordvet Hørsholm </script><b>x</b>"), row("Nordvet Malmö", country="SE")])
    html_page = render(data)
    assert "{{" not in html_page
    assert "<title>No-shows, Friday 6 November 2026</title>" in html_page
    assert '<h1 id="title">Friday 6 November 2026</h1>' in html_page
    assert "Data as of Saturday 7 November 2026, 06:30" in html_page
    # the clinic name cannot end the script element, and the data reads back unchanged
    assert "</script><b>" not in html_page
    assert "Nordvet Malmö" not in html_page and "Malmö" in html_page
    embedded = re.search(r'<script id="data" type="application/json">(.*?)</script>', html_page, re.S).group(1)
    assert json.loads(embedded) == data
    out = tmp_path / "sub" / "no-shows.html"
    write(html_page, out)
    assert out.read_text(encoding="utf-8") == html_page
    assert [p.name for p in out.parent.iterdir()] == ["no-shows.html"]  # no temporary file left


def test_the_page_carries_no_vet_or_patient_fields_and_no_money():
    html_page = render(page([row("A")])).lower()
    for word in ("vet_id", "patient", "dkk", "sek", "cost", "€", "kr."):
        assert word not in html_page


def test_each_day_has_an_all_clinics_row_outside_the_sorted_rows():  # D-056
    day = page([row("A"), row("B", completed=5, no_show=0, assumed=1)])["days"][0]
    assert day["total"] == ["All", "", 15, 1, 1, 17, "11.8%", True, "10.0%", 0.117647, 0.1]
    assert [r[0] for r in day["rows"]] == ["B", "A"]


def test_a_day_without_an_all_clinics_row_fails():
    with pytest.raises(ReportError, match="report_all_clinics_day"):
        page_data([row("A")], [])


def test_the_30_day_header_names_the_earlier_days_the_window_really_holds():
    assert window_label(30) == "Previous 30 days"
    assert window_label(6) == "Previous 6 days"
    assert window_label(1) == "Previous day"
    assert window_label(0) == "No earlier days"
    rows = [row("A"), row("A", day=date(2026, 8, 12), days_30d=6), row("A", day=date(2026, 8, 6), days_30d=0)]
    labels = {d["date"]: d["windowLabel"] for d in page(rows)["days"]}
    assert labels == {"2026-11-06": "Previous 30 days", "2026-08-12": "Previous 6 days",
                      "2026-08-06": "No earlier days"}
