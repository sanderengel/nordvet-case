"""Fetching a run's pages from the API (Phase 3.2, step 2b). Nothing is written here.

Per D-024, D-025 and D-029 a run is:
  * the clinics (one call, no paging); its server time is the run's start;
  * the appointments, one request sequence per month window, from 2026-08-01 to today plus
    60 days, every call with include_deleted=true and limit=500;
  * on an incremental run, every appointment call also has updated_since = the last run's
    start minus 10 minutes.

A request sequence follows next_cursor to the end. A cursor lives 10-15 minutes, a month takes
about a minute, so an expired cursor (410) is rare; when it happens that month starts over
(its pages so far are dropped) and the run's start does not move. Everything is kept in memory
and handed back, so that the caller can write all of it in one transaction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Protocol

from nordvet.api.client import ApiError, HttpResponse
from nordvet.ingest.raw import Page

log = logging.getLogger("nordvet.fetch")

DEFAULT_START = date(2026, 8, 1)  # D-025: the first month with data
LOOKAHEAD_DAYS = 60  # rows run about a month ahead; 60 leaves room
PAGE_LIMIT = 500  # the API's maximum
WATERMARK_MARGIN = timedelta(minutes=10)  # D-024
MAX_RESTARTS = 3  # restarts of one month after a 410


class Getter(Protocol):
    def get(self, path: str, params: dict | None = None) -> HttpResponse: ...


@dataclass(frozen=True)
class FetchResult:
    pages: list[Page]
    started_at: datetime  # server time of the first request, naive UTC (becomes raw.run.started_at)
    windows: int
    restarts: int


class CursorExpired(Exception):
    """A 410: the cursor is too old. Handled here by restarting the month."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Inclusive (from, to) windows, one per calendar month, covering start..end."""
    windows = []
    d = start
    while d <= end:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        windows.append((d, nxt - timedelta(days=1)))
        d = nxt
    return windows


def updated_since_param(last_run_start: datetime) -> str:
    """The updated_since value for an incremental run (D-024): last run's start minus the margin."""
    return (last_run_start - WATERMARK_MARGIN).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_run(
    client: Getter,
    updated_since: str | None = None,
    start: date = DEFAULT_START,
    now: Callable[[], datetime] = utc_now,
) -> FetchResult:
    pages: list[Page] = []

    clinics = _page(client, "clinics", "/v1/clinics", {}, 1, now)
    pages.append(clinics)
    started_at = clinics.server_date
    end = started_at.date() + timedelta(days=LOOKAHEAD_DAYS)

    windows = month_windows(start, end)
    restarts = 0
    for first, last in windows:
        params = {
            "from": first.isoformat(),
            "to": last.isoformat(),
            "include_deleted": "true",
            "limit": str(PAGE_LIMIT),
        }
        if updated_since:
            params["updated_since"] = updated_since
        for attempt in range(MAX_RESTARTS + 1):
            try:
                window_pages = _sequence(client, params, now)
                break
            except CursorExpired:
                restarts += 1
                log.warning("cursor expired in %s..%s: starting that month again", first, last)
        else:
            raise ApiError(f"{first}..{last}: cursor expired {MAX_RESTARTS + 1} times in a row")
        rows = sum(p.rows for p in window_pages)
        log.info("%s..%s: %d rows in %d pages", first, last, rows, len(window_pages))
        pages.extend(window_pages)

    return FetchResult(pages=pages, started_at=started_at, windows=len(windows), restarts=restarts)


# ---------------------------------------------------------------------------------------


def _sequence(client: Getter, params: dict, now: Callable[[], datetime]) -> list[Page]:
    """One month: follow next_cursor to the end."""
    out: list[Page] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        query = dict(params, cursor=cursor) if cursor else dict(params)
        what = f"appointments {params['from']}..{params['to']} page {len(out) + 1}"
        resp = client.get("/v1/appointments", query)
        if resp.status == 410:
            raise CursorExpired
        out.append(_to_page("appointments", params, len(out) + 1, resp, now, what))
        cursor = json.loads(resp.text).get("next_cursor")
        if cursor is None:
            return out
        if not isinstance(cursor, str) or not cursor:
            raise ApiError(f"{what}: next_cursor is not a text or null")
        if cursor in seen:
            raise ApiError(f"{what}: the API repeated a cursor")
        seen.add(cursor)


def _page(client: Getter, endpoint: str, path: str, params: dict, page_no: int, now) -> Page:
    resp = client.get(path, params or None)
    return _to_page(endpoint, params, page_no, resp, now, endpoint)


def _to_page(endpoint: str, params: dict, page_no: int, resp: HttpResponse, now, what: str) -> Page:
    """Check the answer and keep its text exactly as received."""
    if resp.status != 200:
        raise ApiError(f"{what}: HTTP {resp.status}")
    try:
        doc = json.loads(resp.text)
    except ValueError as exc:
        raise ApiError(f"{what}: the answer is not JSON") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise ApiError(f"{what}: the answer has no data list")
    return Page(
        endpoint=endpoint,
        params=dict(params),
        page_no=page_no,
        server_date=_server_date(resp, what),
        fetched_at=now(),
        body=resp.text,
        rows=len(doc["data"]) if endpoint == "appointments" else 0,
    )


def _server_date(resp: HttpResponse, what: str) -> datetime:
    raw = resp.headers.get("date")
    if not raw:
        raise ApiError(f"{what}: the answer has no Date header")
    value = parsedate_to_datetime(raw)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)
