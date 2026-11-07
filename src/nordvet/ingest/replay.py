"""Thin end-to-end (Phase 3.2, step 1): replay a SAVED pull into raw, build staging, check it.

    python -m nordvet.ingest.replay --pull data/raw/20261106T232703Z_pull [--reset] [--max-pages 1]

What it answers (D-030): do real pages go into raw and come out of OPENJSON as typed
staging rows, do the Danish and Swedish letters survive the driver, and how long does the
database side of a full first load take. It reads the pages the Phase 2 tools saved, so it
needs Docker and the SQL Server password but no API credentials and no API calls. The API
side (auth, paging, retries) is the loader's job, in the next step.

The replay is only a test of the database side. The pages it writes are marked with
source 'saved pull ...' in raw.run, and it is not part of the pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from nordvet.config import ConfigError, load_settings, redact
from nordvet.db.connection import connect, ensure_database
from nordvet.ingest.raw import Page, create_tables, write_run
from nordvet.ingest.sqlfiles import read_batches, run_file
from nordvet.ingest.staging import build_staging

DEFAULT_START = "2026-08-01"  # D-025: the first month with data
ACCENT_TEXT = "æøå ÆØÅ ö Ö é ü"  # å is in no clinic name today, but a later clinic could have one


# --- reading the saved pull (pure, tested) ---------------------------------------------


def _naive_utc(value: datetime) -> datetime:
    """Whole seconds, UTC, no tzinfo (what DATETIME2(0) holds)."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _page_from_meta(meta: dict, body_path: Path, endpoint: str, page_no: int) -> Page:
    body = body_path.read_text(encoding="utf-8")
    rows = len(json.loads(body)["data"]) if endpoint == "appointments" else 0
    return Page(
        endpoint=endpoint,
        params={k: v for k, v in meta["params"].items() if k != "cursor"},
        page_no=page_no,
        server_date=_naive_utc(parsedate_to_datetime(meta["response_headers"]["date"])),
        fetched_at=_naive_utc(datetime.fromisoformat(meta["fetched_at_utc"])),
        body=body,
        rows=rows,
    )


def read_appointment_pages(pull_dir: Path, start: str = DEFAULT_START, max_pages: int | None = None) -> list[Page]:
    """The all-rows pages (include_deleted=true) of a saved pull, in the order they were fetched.

    Only HTTP 200 responses count (the folder also holds 429 and 500 answers and their retries).
    Windows that start before `start` are left out, as the loader would.
    """
    pages: list[Page] = []
    seen_per_window: dict[tuple, int] = {}
    for meta_path in sorted(pull_dir.glob("*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        params = meta.get("params", {})
        if meta.get("path") != "/v1/appointments" or meta.get("status") != 200:
            continue
        if params.get("include_deleted") != "true" or params.get("from", "") < start:
            continue
        window = (params.get("from"), params.get("to"))
        seen_per_window[window] = seen_per_window.get(window, 0) + 1
        pages.append(
            _page_from_meta(meta, pull_dir / meta["body_saved_as"], "appointments", seen_per_window[window])
        )
        if max_pages is not None and len(pages) >= max_pages:
            break
    return pages


def pull_started_at(pages: list[Page]) -> datetime:
    """When the saved pull began: the earliest server Date among its appointment pages.

    Used as raw.run.started_at of the replay (D-035): the model is "as of" the latest run start,
    so a replay must carry the time of the pull it replays. Without it the run took the date of
    the one saved clinics response (6 Nov 16:57 UTC) whatever pull was replayed (F1, Phase 4).
    """
    if not pages:
        raise ValueError("a pull needs at least one page")
    return min(p.server_date for p in pages)


def find_clinics_page(raw_dir: Path) -> Page:
    """The newest saved /v1/clinics response in data/raw."""
    best: tuple[str, dict, Path] | None = None
    for meta_path in raw_dir.glob("*/*.meta.json"):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("path") == "/v1/clinics" and meta.get("status") == 200 and not meta.get("params"):
            if meta.get("body_saved_as") and not meta.get("body_is_shape_only"):
                stamp = meta["fetched_at_utc"]
                if best is None or stamp > best[0]:
                    best = (stamp, meta, meta_path.parent / meta["body_saved_as"])
    if best is None:
        raise FileNotFoundError(f"no saved /v1/clinics response under {raw_dir}")
    return _page_from_meta(best[1], best[2], "clinics", 1)


# --- what staging should contain (pure, tested) ----------------------------------------


def split_scheduled(value: str) -> tuple[str, int]:
    """'2026-10-01T08:00+02:00' -> ('2026-10-01T08:00:00', 120): local wall time and offset in minutes."""
    wall, sign, offset = value[:-6], value[-6], value[-5:]
    if len(wall) == 16:
        wall += ":00"
    minutes = int(offset[:2]) * 60 + int(offset[3:])
    return wall, -minutes if sign == "-" else minutes


def norm_utc(value: str | None) -> str | None:
    """'2026-09-30T10:34:11Z' -> '2026-09-30T10:34:11'."""
    if value is None:
        return None
    return value[:-1] if value.endswith("Z") else value


def expected_appointment(row: dict) -> tuple:
    return (
        row["id"], row["clinic_id"], row["vet_id"], row["service"],
        *split_scheduled(row["scheduled_at"]), row["duration_minutes"], row["status"],
        norm_utc(row["created_at"]), norm_utc(row["updated_at"]),
        norm_utc(row.get("cancelled_at")), norm_utc(row.get("deleted_at")),
    )


def expected_appointments(pages: list[Page]) -> dict[str, tuple]:
    """One tuple per id: the highest updated_at wins, on a tie the later page (D-027)."""
    latest: dict[str, tuple[str, dict]] = {}
    for page in pages:
        if page.endpoint != "appointments":
            continue
        for row in json.loads(page.body)["data"]:
            current = latest.get(row["id"])
            if current is None or row["updated_at"] >= current[0]:
                latest[row["id"]] = (row["updated_at"], row)
    return {rid: expected_appointment(row) for rid, (_, row) in latest.items()}


def expected_clinics(page: Page) -> dict[str, tuple]:
    return {
        c["id"]: (c["id"], c["name"], c["city"], c["country"], c["timezone"], int(c["active"]), c["opened_on"])
        for c in json.loads(page.body)["data"]
    }


def compare(expected: dict[str, tuple], actual_rows: list[tuple]) -> list[str]:
    """Differences between what staging should hold and what it holds; empty means equal."""
    actual = {row[0]: tuple(row) for row in actual_rows}
    problems = []
    if len(actual) != len(actual_rows):
        problems.append(f"{len(actual_rows) - len(actual)} duplicate ids in staging")
    missing = expected.keys() - actual.keys()
    extra = actual.keys() - expected.keys()
    if missing:
        problems.append(f"{len(missing)} ids missing from staging, e.g. {sorted(missing)[:3]}")
    if extra:
        problems.append(f"{len(extra)} ids in staging that the pages do not have, e.g. {sorted(extra)[:3]}")
    different = [rid for rid in expected.keys() & actual.keys() if expected[rid] != actual[rid]]
    if different:
        rid = sorted(different)[0]
        problems.append(f"{len(different)} rows differ; first {rid}: expected {expected[rid]}, got {actual[rid]}")
    return problems


# --- the run ---------------------------------------------------------------------------


def _fetch_all(conn, relative_path: str, params: tuple = ()) -> list[tuple]:
    cur = conn.cursor()
    (batch,) = read_batches(relative_path)
    cur.execute(batch, params) if params else cur.execute(batch)
    return [tuple(r) for r in cur.fetchall()]


def run_replay(pull_dir: Path, raw_dir: Path, start: str, reset: bool, max_pages: int | None) -> int:
    settings = load_settings()
    failures: list[str] = []

    t = time.perf_counter()
    pages = read_appointment_pages(pull_dir, start, max_pages)
    clinics = find_clinics_page(raw_dir)
    all_pages = [clinics] + pages  # the clinics response first, as the loader will fetch it
    read_s = time.perf_counter() - t
    if not pages:
        print(f"[FAIL] no all-rows appointment pages found in {pull_dir}")
        return 1
    megabytes = sum(len(p.body.encode("utf-8")) for p in all_pages) / 1e6
    print(f"read {len(pages)} appointment pages + clinics ({megabytes:.1f} MB, "
          f"{sum(p.rows for p in pages)} rows) from disk in {read_s:.1f}s")

    created = ensure_database(settings)
    conn = connect(settings)
    try:
        if reset:
            run_file(conn, "raw/reset_dev.sql")
            conn.commit()
            print("reset: dropped raw and staging tables")
        create_tables(conn)

        t = time.perf_counter()
        started_at = pull_started_at(pages)
        run_id = write_run(conn, f"saved pull {pull_dir.name} (replay)", all_pages, started_at=started_at)
        insert_s = time.perf_counter() - t
        print(f"raw: run {run_id}, {len(all_pages)} pages inserted in {insert_s:.1f}s (one transaction), "
              f"run start {started_at:%Y-%m-%d %H:%M:%S} UTC (the pull's first page)")

        t = time.perf_counter()
        build_staging(conn)
        build_s = time.perf_counter() - t
        print(f"staging: rebuilt from raw with OPENJSON in {build_s:.1f}s")

        # 1. every staging row equals what Python reads from the same JSON
        t = time.perf_counter()
        rows = _fetch_all(conn, "checks/staging_appointment_roundtrip.sql")
        problems = compare(expected_appointments(all_pages), rows)
        print(f"check appointments: {len(rows)} staging rows compared field by field "
              f"in {time.perf_counter() - t:.1f}s -> {'OK' if not problems else 'FAIL'}")
        failures += problems

        # 2. clinics, including the non-ASCII names
        clinic_rows = _fetch_all(conn, "checks/staging_clinic_roundtrip.sql")
        problems = compare(expected_clinics(clinics), clinic_rows)
        letters = sorted({ch for r in clinic_rows for ch in r[1] + r[2] if ord(ch) > 127})
        print(f"check clinics: {len(clinic_rows)} rows, non-ASCII letters in names: {''.join(letters)} "
              f"-> {'OK' if not problems else 'FAIL'}")
        failures += problems

        # 3. the letters the data does not have yet, through parameter -> OPENJSON -> back
        (back,) = _fetch_all(conn, "checks/text_roundtrip.sql", (json.dumps({"v": ACCENT_TEXT}),))[0]
        ok = back == ACCENT_TEXT
        print(f"check text round trip: sent {ACCENT_TEXT!r}, got {back!r} -> {'OK' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"text round trip changed {ACCENT_TEXT!r} into {back!r}")
    except Exception as exc:  # driver and server errors vary; report class and redacted message
        print(f"[FAIL] {type(exc).__name__}: {redact(exc, settings)}")
        return 1
    finally:
        conn.close()

    print(f"database side of a first load: insert {insert_s:.1f}s + staging {build_s:.1f}s "
          f"(reading the files {read_s:.1f}s, not counted in the pipeline)")
    for line in failures:
        print(f"[FAIL] {line}")
    print("RESULT:", "all checks passed" if not failures else f"{len(failures)} problem(s)")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pull", required=True, type=Path, help="a saved pull folder under data/raw")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"), help="where saved responses live")
    parser.add_argument("--start", default=DEFAULT_START, help="first window start (default 2026-08-01)")
    parser.add_argument("--reset", action="store_true", help="drop raw and staging tables first (dev only)")
    parser.add_argument("--max-pages", type=int, default=None, help="only the first N appointment pages")
    args = parser.parse_args(argv)
    try:
        return run_replay(args.pull, args.raw_dir, args.start, args.reset, args.max_pages)
    except ConfigError as exc:
        print(f"[FAIL] config: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
