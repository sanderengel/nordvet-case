"""Exhaustive exploration of the VetOne API (Phase 2). Not the ingestion code.

Three subcommands, meant to be run in this order with a look at the output in
between:

    python -m nordvet.explore.explore_full discover   # endpoints, errors, limits, token
    python -m nordvet.explore.explore_full pull       # every appointment, month by month
    python -m nordvet.explore.explore_full filters    # how from/to, updated_since, clinic_id behave
    python -m nordvet.explore.explore_full edges      # updated_since boundary, unknown clinic, old cursor
    python -m nordvet.explore.explore_full poll       # hours-long: token and cursor lifetime, slowness after idle

Everything lands in data/raw/<UTC timestamp>_<subcommand>/ (gitignored), saved raw
as received, with the same secret handling as nordvet.explore.explore: request
headers are never written and the secret and token are scrubbed.
Each call writes NNNN_<step>.json plus NNNN_<step>.meta.json.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from nordvet.explore.explore import Probe, run_errors
from nordvet.config import ConfigError, load_settings

MAX_RETRIES = 6
DEFAULT_LIMIT = 100  # the API's default page size; the maximum is found by `discover`
OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


# ----------------------------------------------------------------- helpers


def setup(args: argparse.Namespace, name: str, quiet: bool = False) -> Probe | None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return None
    missing = [m for m in settings.missing() if m.startswith("VETONE_")]
    if missing:
        print("Missing in the env file: " + ", ".join(missing))
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out_dir) / f"{stamp}_{name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Saving to {out_dir}\n")
    probe = Probe(settings, out_dir)
    probe.quiet = quiet
    if not probe.authenticate():
        print("Authentication failed; nothing else was tried.")
        return None
    return probe


def get(p: Probe, step: str, path: str, params: dict | str | None = None):
    """GET with the token, retrying what the API is known to do:

    429  wait for Retry-After, then retry
    5xx  (the API answers "temporary error, please retry") or a network error:
         wait 2, 4, 8, ... seconds (at most 30), then retry
    401  authenticate again, once
    Every retry is saved as its own file (<step>_retryN), so nothing is hidden.
    """
    resp, parsed = p.call(step, "GET", path, params=params, token=p.token)
    reauthed = False
    for attempt in range(1, MAX_RETRIES + 1):
        status = None if resp is None else resp.status_code
        if status == 429:
            try:
                wait = int(resp.headers.get("Retry-After", "60")) + 1
            except ValueError:
                wait = 61
            print(f"   429 on {step}: waiting {wait}s (Retry-After), retry {attempt}/{MAX_RETRIES}")
            p.rate_limit_waits += 1
            p.rate_limit_seconds += wait
        elif status is None or status >= 500:
            wait = min(2**attempt, 30)
            print(f"   {status or 'network error'} on {step}: waiting {wait}s, retry {attempt}/{MAX_RETRIES}")
            p.server_retries += 1
        elif status == 401 and not reauthed:
            print(f"   401 on {step}: authenticating again and retrying")
            reauthed = True
            p.reauths += 1
            wait = 0
            if not p.authenticate():
                break
        else:
            break
        time.sleep(wait)
        resp, parsed = p.call(f"{step}_retry{attempt}", "GET", path, params=params, token=p.token)
    return resp, parsed


def fetch_all(p: Probe, step: str, params: dict, max_pages: int, limit: int, raw_query: str | None = None):
    """Follow next_cursor to the end (or the page cap). Returns (rows, pages, note)."""
    rows: list[dict] = []
    pages = 0
    cursor = None
    seen: set[str] = set()
    note = ""
    while pages < max_pages:
        if raw_query is not None:
            query: dict | str = f"{raw_query}&limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        else:
            query = dict(params, limit=limit)
            if cursor:
                query["cursor"] = cursor
        resp, parsed = get(p, f"{step}_p{pages + 1:03d}", "/v1/appointments", query)
        if (
            resp is None
            or resp.status_code != 200
            or not isinstance(parsed, dict)
            or not isinstance(parsed.get("data"), list)
        ):
            note = f"STOPPED: bad response on page {pages + 1}"
            break
        pages += 1
        rows.extend(parsed["data"])
        cursor = parsed.get("next_cursor")
        if not cursor:
            break
        if cursor in seen:
            note = "STOPPED: repeated cursor"
            break
        seen.add(cursor)
    else:
        note = "STOPPED: page cap reached"
    return rows, pages, note


def offset_of(ts: str | None) -> str:
    m = OFFSET_RE.search(ts or "")
    return m.group(1) if m else "none"


def span(values: list[str]) -> str:
    return f"{min(values)} .. {max(values)}" if values else "-"


def month_windows(start: date, end: date):
    d = date(start.year, start.month, 1)
    while d <= end:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        yield d, nxt
        d = nxt


def jwt_info(token: str) -> dict:
    """Header algorithm, claim NAMES and the time claims. Other claim values are not shown."""
    parts = token.split(".")
    if len(parts) != 3:
        return {"is_jwt": False}

    def decode(part: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

    try:
        header, payload = decode(parts[0]), decode(parts[1])
    except ValueError:
        return {"is_jwt": False}
    info: dict = {"is_jwt": True, "alg": header.get("alg"), "claims": sorted(payload)}
    for key in ("iat", "nbf", "exp"):
        if isinstance(payload.get(key), (int, float)):
            info[key] = datetime.fromtimestamp(payload[key], timezone.utc).isoformat(timespec="seconds")
    if isinstance(payload.get("exp"), (int, float)) and isinstance(payload.get("iat"), (int, float)):
        info["lifetime_seconds"] = int(payload["exp"] - payload["iat"])
    return info


# ----------------------------------------------------------------- discover


def discover(args: argparse.Namespace) -> int:
    p = setup(args, "discover")
    if p is None:
        return 1

    print("-- no authentication --")
    for path in ["/", "/docs", "/openapi.json", "/openapi.yaml", "/swagger.json", "/redoc", "/health", "/v1"]:
        step = "noauth_" + (re.sub(r"\W+", "_", path).strip("_") or "root")
        p.call(step, "GET", path)

    print("\n-- with the token: other resources the docs do not mention --")
    for path in ["/openapi.json", "/v1/vets", "/v1/patients", "/v1/services", "/v1/clinics/CL-420"]:
        get(p, "auth_" + re.sub(r"\W+", "_", path).strip("_"), path)
    _, first = get(p, "one_appointment", "/v1/appointments", {"limit": 1})
    if isinstance(first, dict) and first.get("data"):
        apt_id = first["data"][0]["id"]
        get(p, "appointment_by_id", f"/v1/appointments/{apt_id}")

    print("\n-- page size: how large can limit be? --")
    for limit in (100, 200, 500, 1000, 5000):
        get(p, f"limit_{limit}", "/v1/appointments", {"limit": limit})

    print("\n-- token --")
    info = jwt_info(p.token or "")
    print(json.dumps(info, indent=2))
    (p.out_dir / "token_claims.json").write_text(json.dumps(info, indent=2))

    run_errors(p)
    print(f"\nDone. Files are in {p.out_dir}  (re-authentications needed: {p.reauths})")
    return 0


# ----------------------------------------------------------------- pull


def pull(args: argparse.Namespace) -> int:
    p = setup(args, "pull", quiet=True)
    if p is None:
        return 1
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today() + timedelta(days=120)
    print(f"Windows: {start} .. {end}, month by month, limit={args.limit}, page cap {args.max_pages} in total")
    print("Each window is [first of month, first of next month], so one day overlaps on purpose;")
    print("duplicates across windows are handled when profiling.\n")

    summary: list[dict] = []
    ids: dict[str, set[str]] = {"normal": set(), "deleted": set()}
    rows_by_id: dict[str, dict] = {}
    pages_used = 0
    started = time.monotonic()

    for lo, hi in month_windows(start, end):
        for variant in ("normal", "deleted"):
            if pages_used >= args.max_pages:
                print("Total page cap reached; stopping.")
                break
            params = {"from": lo.isoformat(), "to": hi.isoformat()}
            if variant == "deleted":
                params["include_deleted"] = "true"
            t0 = time.monotonic()
            rows, pages, note = fetch_all(
                p, f"pull_{variant}_{lo:%Y-%m}", params, args.max_pages - pages_used, args.limit
            )
            pages_used += pages
            sched = [r["scheduled_at"] for r in rows if r.get("scheduled_at")]
            entry = {
                "window": f"{lo:%Y-%m}",
                "variant": variant,
                "rows": len(rows),
                "pages": pages,
                "scheduled_span": span(sched),
                "status_counts": dict(Counter(r.get("status") for r in rows)),
                "note": note,
            }
            summary.append(entry)
            for r in rows:
                ids[variant].add(r["id"])
                if variant == "deleted":
                    rows_by_id.setdefault(r["id"], r)
            print(
                f"{entry['window']} {variant:<8} rows={len(rows):>6} pages={pages:>4} "
                f"{time.monotonic() - t0:5.1f}s  {entry['scheduled_span']}  {note}"
            )
            (p.out_dir / "pull_summary.json").write_text(json.dumps(summary, indent=2))

    extra = ids["deleted"] - ids["normal"]
    missing = ids["normal"] - ids["deleted"]
    print(f"\nDistinct ids: normal={len(ids['normal'])}  with include_deleted={len(ids['deleted'])}")
    print(f"Only with include_deleted: {len(extra)}   Only without it: {len(missing)}")
    if extra:
        sample = [rows_by_id[i] for i in sorted(extra)[:20]]
        (p.out_dir / "include_deleted_extras_sample.json").write_text(json.dumps(sample, indent=2))
        print("Statuses of the extra rows:", dict(Counter(rows_by_id[i].get("status") for i in extra)))
        print("Fields on extra rows:", sorted({k for i in extra for k in rows_by_id[i]}))
        print("Saved the first 20 as include_deleted_extras_sample.json")
    print(
        f"\nDone in {time.monotonic() - started:.0f}s, {pages_used} pages, "
        f"{p.reauths} re-authentications, {p.rate_limit_waits} rate-limit waits "
        f"({p.rate_limit_seconds}s waiting), {p.server_retries} retries after 5xx/network errors. "
        f"Files are in {p.out_dir}"
    )
    return 0


# ----------------------------------------------------------------- filters


def describe(label: str, rows: list[dict], pages: int, note: str, base: set[str] | None) -> dict:
    sched = [r["scheduled_at"] for r in rows if r.get("scheduled_at")]
    upd = [r["updated_at"] for r in rows if r.get("updated_at")]
    overlap = f" overlap_with_day={len({r['id'] for r in rows} & base)}" if base is not None else ""
    print(
        f"{label:<22} rows={len(rows):>6} pages={pages:>3}{overlap}\n"
        f"{'':<22} scheduled {span(sched)}  offsets={dict(Counter(offset_of(s) for s in sched))}\n"
        f"{'':<22} updated   {span(upd)}  {note}"
    )
    return {"label": label, "rows": len(rows), "pages": pages, "scheduled_span": span(sched), "note": note}


def filters(args: argparse.Namespace) -> int:
    p = setup(args, "filters", quiet=True)
    if p is None:
        return 1
    day = date.fromisoformat(args.day)
    one = timedelta(days=1)
    results: list[dict] = []

    def run(label: str, params: dict, base: set[str] | None = None, raw_query: str | None = None):
        rows, pages, note = fetch_all(p, f"f_{label}", params, args.max_pages, args.limit, raw_query)
        results.append(describe(label, rows, pages, note, base))
        return rows

    print(f"== day boundaries around {day}: is `to` inclusive? ==")
    base_rows = run("day_from_to_same", {"from": day.isoformat(), "to": day.isoformat()})
    base = {r["id"] for r in base_rows}
    run("day_to_next_day", {"from": day.isoformat(), "to": (day + one).isoformat()}, base)
    run("day_from_prev_day", {"from": (day - one).isoformat(), "to": day.isoformat()}, base)
    again = run("day_same_again", {"from": day.isoformat(), "to": day.isoformat()}, base)
    print(f"   same query twice, identical order: {[r['id'] for r in again] == [r['id'] for r in base_rows]}")
    run("from_only", {"from": day.isoformat()})
    run("to_only", {"to": day.isoformat()})

    print(f"\n== daylight saving change ({args.dst_from} .. {args.dst_to}) ==")
    run("dst_window", {"from": args.dst_from, "to": args.dst_to})

    print(f"\n== clinic_id ({args.clinic}) ==")
    clinic_rows = run("clinic_on_day", {"from": day.isoformat(), "to": day.isoformat(), "clinic_id": args.clinic}, base)
    expected = sum(1 for r in base_rows if r.get("clinic_id") == args.clinic)
    print(f"   all rows are that clinic: {all(r.get('clinic_id') == args.clinic for r in clinic_rows)}; "
          f"same count as filtering the day by hand: {len(clinic_rows) == expected}")
    run("inactive_clinic_CL-420", {"from": "2026-01-01", "to": date.today().isoformat(), "clinic_id": "CL-420"})

    print("\n== updated_since: the same instant written five ways ==")
    t = (datetime.now(timezone.utc) - timedelta(hours=36)).replace(minute=0, second=0, microsecond=0)
    local = t + timedelta(hours=1)
    forms = {
        "us_zulu": ({"updated_since": t.strftime("%Y-%m-%dT%H:%M:%SZ")}, None),
        "us_plus00": ({"updated_since": t.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"}, None),
        "us_plus01_local": ({"updated_since": local.strftime("%Y-%m-%dT%H:%M:%S") + "+01:00"}, None),
        "us_naive": ({"updated_since": t.strftime("%Y-%m-%dT%H:%M:%S")}, None),
        "us_plus_unencoded": ({}, "updated_since=" + t.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"),
    }
    print(f"   instant used: {t.isoformat()} (UTC)")
    reference: set[str] | None = None
    for label, (params, raw) in forms.items():
        rows = run(label, params, None, raw)
        got = {r["id"] for r in rows}
        before, equal = 0, 0
        for r in rows:
            try:
                u = datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
            except (KeyError, ValueError, AttributeError):
                continue
            before += u < t
            equal += u == t
        print(f"{'':<22} updated_at before instant: {before} (should be 0); exactly at instant: {equal}")
        if reference is None:
            reference = got
        else:
            print(f"{'':<22} vs us_zulu: only here={len(got - reference)}  only in us_zulu={len(reference - got)}")

    (p.out_dir / "filters_summary.json").write_text(json.dumps(results, indent=2))
    print(f"\nDone. Files are in {p.out_dir}  (re-authentications: {p.reauths}, rate-limit waits: {p.rate_limit_waits}, 5xx retries: {p.server_retries})")
    return 0


# ----------------------------------------------------------------- edges


def one(p: Probe, label: str, params: dict) -> object:
    """One call, one printed line: status, rows and the API's error text."""
    resp, parsed = get(p, f"e_{label}", "/v1/appointments", params)
    status = None if resp is None else resp.status_code
    rows = len(parsed["data"]) if isinstance(parsed, dict) and isinstance(parsed.get("data"), list) else None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    print(f"{label:<30} status={status} rows={rows}" + (f" error={error}" if error else ""))
    return parsed


def zulu(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def edges(args: argparse.Namespace) -> int:
    """The remaining filter questions: updated_since boundaries and combinations,
    unknown clinic ids, and reuse of an old cursor."""
    p = setup(args, "edges", quiet=True)
    if p is None:
        return 1
    day = date.fromisoformat(args.day).isoformat()
    day_params = {"from": day, "to": day}

    print(f"== 1. updated_since exactly at a row's updated_at (rows of {day}) ==")
    rows, _, note = fetch_all(p, "e_day", day_params, args.max_pages, args.limit)
    print(f"rows that day: {len(rows)} {note}")
    if not rows:
        print("No rows that day; pick another with --day.")
        return 1
    ref = max(rows, key=lambda r: r["updated_at"])
    u = ref["updated_at"]
    u_dt = datetime.strptime(u, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    ge = {r["id"] for r in rows if r["updated_at"] >= u}
    gt = {r["id"] for r in rows if r["updated_at"] > u}
    got, _, _ = fetch_all(p, "e_at_instant", dict(day_params, updated_since=u), args.max_pages, args.limit)
    got_ids = {r["id"] for r in got}
    print(f"instant used: {u}; rows with exactly that updated_at: {len(ge) - len(gt)}")
    print(
        "   result equals '>= instant':",
        got_ids == ge,
        "| equals '> instant':",
        got_ids == gt,
        f"| rows={len(got_ids)}",
    )
    after, _, _ = fetch_all(
        p, "e_one_second_later", dict(day_params, updated_since=zulu(u_dt + timedelta(seconds=1))), args.max_pages, args.limit
    )
    print("   one second later: reference row excluded:", ref["id"] not in {r["id"] for r in after})

    print("\n== 2. updated_since with and without include_deleted ==")
    t0 = (datetime.now(timezone.utc) - timedelta(hours=36)).replace(minute=0, second=0, microsecond=0)
    since = {"updated_since": zulu(t0)}
    normal, _, n1 = fetch_all(p, "e_since_normal", since, args.max_pages, args.limit)
    withdel, _, n2 = fetch_all(p, "e_since_deleted", dict(since, include_deleted="true"), args.max_pages, args.limit)
    nid = {r["id"] for r in normal}
    extra = [r for r in withdel if r["id"] not in nid]
    print(f"instant {zulu(t0)}: normal={len(normal)} {n1} | with include_deleted={len(withdel)} {n2} | only with the flag={len(extra)}")
    if extra:
        print(
            "   extras: all have deleted_at:",
            all("deleted_at" in r for r in extra),
            "| updated_at >= instant for all:",
            all(r["updated_at"] >= zulu(t0) for r in extra),
            "| statuses:",
            dict(Counter(r["status"] for r in extra)),
        )

    print("\n== 3. updated_since combined with from/to ==")
    combo, _, _ = fetch_all(p, "e_since_and_day", dict(day_params, **since), args.max_pages, args.limit)
    expected = {r["id"] for r in rows if r["updated_at"] >= zulu(t0)}
    same = {r["id"] for r in combo} == expected
    print(f"rows={len(combo)} expected (that day's rows updated since): {len(expected)} | identical sets: {same}")

    print("\n== 4. clinic_id edge cases ==")
    one(p, "clinic_unknown_CL-999", dict(day_params, clinic_id="CL-999", limit=10))
    one(p, "clinic_lowercase_cl-517", dict(day_params, clinic_id="cl-517", limit=10))
    one(p, "clinic_empty", dict(day_params, clinic_id="", limit=10))

    print("\n== 5. reusing an old cursor ==")
    candidates = sorted(Path(args.out_dir).glob("*/*_appointments_limit10.json"))
    candidates = [c for c in candidates if not c.name.endswith(".meta.json")]
    old_cursor = None
    for path in candidates:
        try:
            old_cursor = json.loads(path.read_bytes()).get("next_cursor")
            meta = json.loads(path.with_name(path.name[:-5] + ".meta.json").read_text())
            fetched = datetime.fromisoformat(meta["fetched_at_utc"])
        except (ValueError, KeyError, OSError):
            continue
        if old_cursor:
            age = (datetime.now(timezone.utc) - fetched).total_seconds() / 60
            print(f"cursor from {path.parent.name}, issued {age:.0f} minutes ago (original call: limit=10)")
            break
    if old_cursor:
        one(p, "old_cursor_same_params", {"limit": 10, "cursor": old_cursor})
        one(p, "old_cursor_other_limit", {"limit": 200, "cursor": old_cursor})
        one(p, "old_cursor_other_filter", {"limit": 10, "clinic_id": "CL-517", "cursor": old_cursor})
    else:
        print("No saved cursor found in data/raw.")

    print(f"\nDone. Files are in {p.out_dir}  (re-authentications: {p.reauths}, rate-limit waits: {p.rate_limit_waits}, 5xx retries: {p.server_retries})")
    return 0


def call_retrying(p: Probe, step: str, path: str, params: dict | None, token: str | None):
    """One call, retried on 429, 5xx and network errors only, so a 401 or 410 comes back as it is."""
    resp, parsed = p.call(step, "GET", path, params=params, token=token)
    for attempt in range(1, 4):
        status = None if resp is None else resp.status_code
        if status == 429:
            try:
                wait = int(resp.headers.get("Retry-After", "60")) + 1
            except ValueError:
                wait = 61
        elif status is None or status >= 500:
            wait = 5 * attempt
        else:
            break
        time.sleep(wait)
        resp, parsed = p.call(f"{step}_retry{attempt}", "GET", path, params=params, token=token)
    return resp, parsed


def poll(args: argparse.Namespace) -> int:
    """Hours-long, a handful of calls at each checkpoint: how long the first token stays valid,
    how long the first cursor stays valid (tested with a fresh token each time), and how long
    the first call takes after the API has been idle since the previous checkpoint.

    Keep the machine awake while it runs (macOS: prefix the command with `caffeinate -i`).
    Results are written to poll_summary.json after every checkpoint, so stopping early keeps them.
    """
    p = setup(args, "poll")
    if p is None:
        return 1
    first_token = p.token
    day = (date.today() - timedelta(days=2)).isoformat()
    page_params = {"from": day, "to": day, "limit": 10}
    resp, parsed = call_retrying(p, "first_page", "/v1/appointments", page_params, first_token)
    cursor = parsed.get("next_cursor") if isinstance(parsed, dict) else None
    if not cursor:
        print("The first page had no next_cursor; pick a busier day.")
        return 1
    marks = sorted(int(m) for m in args.minutes.split(","))
    print(f"Token and cursor issued now. Checkpoints (minutes): {marks}. This runs for about {marks[-1] / 60:.1f} h.\n")
    started = time.monotonic()
    results: list[dict] = []
    token_dead_at = cursor_dead_at = None
    for minute in marks:
        wait = started + minute * 60 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        row: dict = {"minute": minute, "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        t0 = time.monotonic()
        r, _ = call_retrying(p, f"m{minute:04d}_health", "/health", None, None)
        row["first_call_after_idle_s"] = round(time.monotonic() - t0, 2)
        row["health_status"] = None if r is None else r.status_code
        if token_dead_at is None:
            r, _ = call_retrying(p, f"m{minute:04d}_first_token", "/v1/appointments", page_params, first_token)
            row["first_token_status"] = None if r is None else r.status_code
            if r is not None and r.status_code == 401:
                token_dead_at = minute
        if cursor_dead_at is None:
            if p.authenticate():
                r, _ = call_retrying(p, f"m{minute:04d}_first_cursor", "/v1/appointments", {"limit": 10, "cursor": cursor}, p.token)
                row["first_cursor_status"] = None if r is None else r.status_code
                if r is not None and r.status_code in (400, 410):
                    cursor_dead_at = minute
        results.append(row)
        print(f"   minute {minute:>4}: " + ", ".join(f"{k}={v}" for k, v in row.items() if k != "minute"))
        summary = {
            "first_token_rejected_at_minute": token_dead_at,
            "first_cursor_rejected_at_minute": cursor_dead_at,
            "checkpoints": results,
        }
        (p.out_dir / "poll_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nDone. First token rejected at minute: {token_dead_at} (None = still valid at the end). "
          f"First cursor rejected at minute: {cursor_dead_at}. Summary in {p.out_dir / 'poll_summary.json'}")
    return 0


# ----------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out-dir", default="data/raw")
    parser = argparse.ArgumentParser(prog="nordvet.explore.explore_full", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", parents=[common], help="endpoints, errors, page size, token")

    sp = sub.add_parser("pull", parents=[common], help="all appointments, month by month")
    sp.add_argument("--start", default="2025-06-01", help="first month to ask for (earlier months were verified empty)")
    sp.add_argument("--end", default=None, help="last date to ask for (default: today + 120 days)")
    sp.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    sp.add_argument("--max-pages", type=int, default=4000, help="safety cap on pages in total")

    sf = sub.add_parser("filters", parents=[common], help="filter behaviour on chosen days")
    sf.add_argument("--day", default=(date.today() - timedelta(days=2)).isoformat(), help="a day that has data")
    sf.add_argument("--clinic", default="CL-517")
    sf.add_argument("--dst-from", default="2026-10-24")
    sf.add_argument("--dst-to", default="2026-10-26")
    sf.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    sf.add_argument("--max-pages", type=int, default=100, help="safety cap per query")

    se = sub.add_parser("edges", parents=[common], help="updated_since boundaries, unknown clinic, old cursor")
    se.add_argument("--day", default=(date.today() - timedelta(days=2)).isoformat(), help="a day that has data")
    se.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    se.add_argument("--max-pages", type=int, default=50, help="safety cap per query")

    sq = sub.add_parser("poll", parents=[common], help="hours-long: token and cursor lifetime, slowness after idle")
    sq.add_argument("--minutes", default="2,5,10,15,20,30,40,50,60,90,120,180,240,300,360,420,480",
                    help="checkpoints in minutes after the token and cursor were issued")

    args = parser.parse_args(argv)
    return {"discover": discover, "pull": pull, "filters": filters, "edges": edges, "poll": poll}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
