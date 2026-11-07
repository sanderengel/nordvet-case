"""Timestamps, clocks and late-visible rows (Phase 3.1: questions 1, 2, 3, 5 and 18). Offline.

    python -m nordvet.explore.profile_watermark                  # every usable snapshot in data/raw
    python -m nordvet.explore.profile_watermark --root data/raw --margin 60

Reads the raw pages that `explore_full pull` saved, including each request's meta file
(the server's `Date` header, our own fetch time and the response time), and answers:

  Q3   How far apart are the API's clock and ours?
  Q18  How long do 500-row pages take to come back?
  Q2   How are `updated_at` and the other stamps recorded (whole minutes or exact
       seconds, per kind of change), and does any stamp lie ahead of the server's clock?
  Q1   Are rows ever visible late: new or changed in a later snapshot although their
       stamp says they existed before the earlier snapshot read their month window?
  Q5   Can `updated_at` go backwards, and what else changes in a row besides status?

Comparisons use the all-rows variant (`include_deleted=true`, Q8) and only month windows
that are complete in both snapshots. Each window is read page by page over a minute or
so, so a row is compared with the time its own window was read, taken from the server's
`Date` header (the same clock the stamps are assumed to come from, which Q3 checks).
The report is saved as watermark_report.txt in the root folder.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics as st
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from nordvet.explore.profile_raw import Report

UTC = timezone.utc
PAGE_STEP = re.compile(r"^pull_(normal|deleted)_(\d{4}-\d{2})_p\d+(?:_retry\d+)?$")
STAMP_FIELDS = ("created_at", "updated_at", "cancelled_at", "deleted_at")
MUTABLE_FIELDS = {"status", "updated_at", "cancelled_at", "deleted_at"}


# ----------------------------------------------------------------- pure helpers


def parse_stamp(ts: str) -> datetime:
    """'2026-11-06T23:31:18Z' -> aware UTC datetime."""
    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)


def parse_http_date(text: str) -> datetime:
    """'Fri, 06 Nov 2026 23:31:18 GMT' -> aware UTC datetime."""
    return parsedate_to_datetime(text).astimezone(UTC)


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0-100) of a non-empty list."""
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(q / 100 * len(ordered)) - 1))
    return ordered[rank]


def classify_stamp(stamp: datetime, first: datetime, last: datetime, margin: float) -> str:
    """Where a row's stamp falls against the interval [first, last] in which a window was read.

    after        later than the end of the read: the row can legitimately be missing from it
    during       inside the read: it may or may not have been there when its page was read
    just_before  within `margin` seconds before the read began: ambiguous if stamps are
                 whole minutes (the true write time can be up to a minute later)
    before       earlier than that: the row should have been visible
    """
    if stamp > last:
        return "after"
    if stamp >= first:
        return "during"
    if (first - stamp).total_seconds() <= margin:
        return "just_before"
    return "before"


def candidate_windows(scheduled_at: str) -> list[str]:
    """Month windows that can contain an appointment. A window is `from` the 1st `to` the 1st
    of the next month, both inclusive, so day 1 of a month sits in two windows."""
    year, month, day = int(scheduled_at[:4]), int(scheduled_at[5:7]), int(scheduled_at[8:10])
    out = [f"{year:04d}-{month:02d}"]
    if day == 1:
        py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
        out.append(f"{py:04d}-{pm:02d}")
    return out


def diff_rows(old: dict[str, dict], new: dict[str, dict]) -> tuple[list[str], list[str], list[tuple[str, dict, dict]]]:
    """(ids only in new, ids only in old, [(id, old row, new row)] whose content differs)."""
    new_ids = sorted(set(new) - set(old))
    gone_ids = sorted(set(old) - set(new))
    changed = [(i, old[i], new[i]) for i in sorted(set(old) & set(new)) if old[i] != new[i]]
    return new_ids, gone_ids, changed


def seconds_zero_share(rows: list[dict], field: str) -> tuple[int, float]:
    """(rows that have the field, share of them whose seconds are 00)."""
    stamps = [r[field] for r in rows if field in r]
    if not stamps:
        return 0, 0.0
    return len(stamps), sum(1 for s in stamps if s[17:19] == "00") / len(stamps)


def created_minus_updated(rows: list[dict]) -> Counter:
    """For rows never touched after creation, how `updated_at` relates to `created_at`.

    equal             identical to the second
    upd_floor         `updated_at` is `created_at` cut down to the minute (1 to 59 s earlier)
    upd_later_<60s    `updated_at` is 1 to 59 s after `created_at`
    apart             60 s or more apart (the row was touched, or the stamps are unrelated)
    """
    out: Counter = Counter()
    for r in rows:
        delta = (parse_stamp(r["created_at"]) - parse_stamp(r["updated_at"])).total_seconds()
        if delta == 0:
            out["equal"] += 1
        elif 0 < delta < 60:
            out["upd_floor"] += 1
        elif -60 < delta < 0:
            out["upd_later_<60s"] += 1
        else:
            out["apart"] += 1
    return out


# ----------------------------------------------------------------- loading


@dataclass
class Page:
    window: str
    server_time: datetime  # the response's Date header (second resolution)
    our_time: datetime  # when our code finished saving the response
    elapsed: float
    rows: list[dict]


@dataclass
class Snapshot:
    name: str
    complete: set[str]
    pages: dict[str, list[Page]]

    def usable_windows(self) -> set[str]:
        return {w for w in self.complete if self.pages.get(w)}

    def window_times(self, window: str) -> tuple[datetime, datetime]:
        times = [p.server_time for p in self.pages[window]]
        return min(times), max(times)

    def rows(self, windows: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for w in windows:
            for page in self.pages.get(w, []):
                for row in page.rows:
                    out[row["id"]] = row
        return out


def read_metas(folder: Path):
    for path in sorted(folder.glob("*.meta.json")):
        try:
            yield json.loads(path.read_text())
        except ValueError:
            continue


def load_snapshot(folder: Path) -> Snapshot | None:
    summary_path = folder / "pull_summary.json"
    if not summary_path.exists():
        return None
    summary = {(e["window"], e["variant"]): e for e in json.loads(summary_path.read_text())}
    complete = {w for (w, v), e in summary.items() if v == "deleted" and not e.get("note")}
    pages: dict[str, list[Page]] = defaultdict(list)
    for meta in read_metas(folder):
        m = PAGE_STEP.match(meta.get("step", ""))
        header = (meta.get("response_headers") or {}).get("date")
        if not m or m.group(1) != "deleted" or meta.get("status") != 200 or not header:
            continue
        try:
            body = json.loads((folder / meta["body_saved_as"]).read_bytes())
        except (ValueError, OSError):
            continue
        if not (isinstance(body, dict) and isinstance(body.get("data"), list)):
            continue
        pages[m.group(2)].append(
            Page(m.group(2), parse_http_date(header), datetime.fromisoformat(meta["fetched_at_utc"]), meta["elapsed_s"], body["data"])
        )
    snap = Snapshot(folder.name, complete, dict(pages))
    has_rows = any(page.rows for w in snap.usable_windows() for page in snap.pages[w])
    return snap if has_rows else None


# ----------------------------------------------------------------- the pair analysis


def pair_findings(p: Snapshot, q: Snapshot, windows: list[str], margin: float) -> dict:
    """Compare an earlier snapshot p with a later one q over the given complete windows."""
    p_rows, q_rows = p.rows(windows), q.rows(windows)
    new_ids, gone_ids, changed = diff_rows(p_rows, q_rows)

    def read_span(row: dict) -> tuple[datetime, datetime]:
        wins = [w for w in candidate_windows(row["scheduled_at"]) if w in windows] or windows
        spans = [p.window_times(w) for w in wins]
        return min(s[0] for s in spans), max(s[1] for s in spans)

    f: dict = {
        "rows_p": len(p_rows), "rows_q": len(q_rows), "new": len(new_ids), "gone": len(gone_ids), "changed": len(changed),
        "new_created": Counter(), "new_updated": Counter(), "changed_updated": Counter(),
        "backwards": 0, "not_bumped": 0, "undeleted": 0,
        "fields": Counter(), "other_fields": Counter(), "transitions": Counter(),
        "examples": [], "after_gap_min": None,
    }
    after_gaps: list[float] = []

    def note(kind: str, rid: str, row: dict, stamp: datetime, first: datetime, last: datetime) -> None:
        if len(f["examples"]) < 8:
            f["examples"].append(
                f"{kind} {rid} status={row['status']} stamp={stamp:%Y-%m-%dT%H:%M:%SZ} read {first:%H:%M:%S}-{last:%H:%M:%S}"
            )

    for rid in new_ids:
        row = q_rows[rid]
        first, last = read_span(row)
        created, updated = parse_stamp(row["created_at"]), parse_stamp(row["updated_at"])
        c, u = classify_stamp(created, first, last, margin), classify_stamp(updated, first, last, margin)
        f["new_created"][c] += 1
        f["new_updated"][u] += 1
        if c in ("before", "just_before"):
            note(f"new/created_at/{c}", rid, row, created, first, last)
        if u in ("before", "just_before"):
            note(f"new/updated_at/{u}", rid, row, updated, first, last)
        if u == "after":
            after_gaps.append((updated - last).total_seconds())

    for rid, old, new in changed:
        first, last = read_span(new)
        t_old, t_new = parse_stamp(old["updated_at"]), parse_stamp(new["updated_at"])
        if t_new < t_old:
            f["backwards"] += 1
        elif t_new == t_old:
            f["not_bumped"] += 1
        cls = classify_stamp(t_new, first, last, margin)
        f["changed_updated"][cls] += 1
        if cls in ("before", "just_before"):
            note(f"changed/updated_at/{cls}", rid, new, t_new, first, last)
        if cls == "after":
            after_gaps.append((t_new - last).total_seconds())
        diff_fields = [k for k in set(old) | set(new) if old.get(k) != new.get(k)]
        f["fields"].update(diff_fields)
        f["other_fields"].update(k for k in diff_fields if k not in MUTABLE_FIELDS)
        if old["status"] != new["status"]:
            f["transitions"][(old["status"], new["status"])] += 1
        if "deleted_at" in old and "deleted_at" not in new:
            f["undeleted"] += 1
    f["after_gap_min"] = min(after_gaps) if after_gaps else None
    return f


def stamp_findings(snap: Snapshot) -> dict:
    """Resolution of the stamps in one snapshot, and any stamp ahead of the server's clock."""
    windows = sorted(snap.usable_windows())
    rows = list(snap.rows(windows).values())
    live = [r for r in rows if "deleted_at" not in r]
    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in live:
        by_group[r["status"]].append(r)
    by_group["(soft-deleted)"] = [r for r in rows if "deleted_at" in r]

    ahead: Counter = Counter()
    worst: dict[str, tuple[float, str]] = {}
    closest: float | None = None
    for w in windows:
        for page in snap.pages[w]:
            for r in page.rows:
                for field in STAMP_FIELDS:
                    if field not in r:
                        continue
                    gap = (page.server_time - parse_stamp(r[field])).total_seconds()  # >0: stamp is in the past
                    if gap < 0:
                        ahead[field] += 1
                        if field not in worst or gap < worst[field][0]:
                            worst[field] = (gap, f"{r['id']} {r[field]} vs Date {page.server_time:%H:%M:%S}")
                    elif field == "updated_at" and (closest is None or gap < closest):
                        closest = gap
    booked_only = [r for r in by_group.get("booked", []) if "cancelled_at" not in r]
    cancelled = by_group.get("cancelled", [])
    return {
        "rows": len(rows), "windows": windows, "groups": by_group,
        "created_vs_updated": created_minus_updated(booked_only), "booked_n": len(booked_only),
        "cancelled_equal": sum(1 for r in cancelled if r["updated_at"] == r["cancelled_at"]), "cancelled_n": len(cancelled),
        "ahead": ahead, "worst": worst, "closest": closest,
    }


# ----------------------------------------------------------------- clocks and speed (all folders)


def skew_by_folder(root: Path) -> dict[str, list[float]]:
    """our clock minus the server's, in seconds, for every saved request that has a Date header."""
    out: dict[str, list[float]] = {}
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        values = []
        for meta in read_metas(folder):
            header = (meta.get("response_headers") or {}).get("date")
            if header and meta.get("fetched_at_utc"):
                values.append((datetime.fromisoformat(meta["fetched_at_utc"]) - parse_http_date(header)).total_seconds())
        if values:
            out[folder.name] = values
    return out


def timings(root: Path) -> dict[int, list[float]]:
    """Response times of the appointment pages in the pull folders, by HTTP status."""
    out: dict[int, list[float]] = defaultdict(list)
    for folder in sorted(root.glob("*_pull")):
        for meta in read_metas(folder):
            if meta.get("path") == "/v1/appointments" and "elapsed_s" in meta:
                out[meta["status"]].append(meta["elapsed_s"])
    return out


# ----------------------------------------------------------------- the report


def fmt_counter(c: Counter, order: list[str]) -> str:
    return "  ".join(f"{k} {c.get(k, 0)}" for k in order)


def report_clocks(root: Path, out: Report) -> None:
    out("=" * 72)
    out("Q3  CLOCKS: our clock minus the server's `Date` header, per request, in seconds")
    out("    (`fetched_at_utc` is stamped after the response is saved, so it runs a little ahead;")
    out("     both clocks have second resolution)")
    allv: list[float] = []
    for name, v in skew_by_folder(root).items():
        allv += v
        out(f"   {name:34s} n={len(v):4d}  min {min(v):5.1f}  median {st.median(v):4.1f}  p95 {percentile(v, 95):4.1f}  max {max(v):5.1f}")
    if allv:
        out(f"   all folders: n={len(allv)}  min {min(allv):.1f}  median {st.median(allv):.1f}  max {max(allv):.1f}  "
            f"requests where we are BEHIND the server: {sum(1 for x in allv if x < 0)}")
    out("\nQ18  RESPONSE TIMES of /v1/appointments pages in the pull folders (seconds, as measured by our client)")
    for status, v in sorted(timings(root).items()):
        out(f"   HTTP {status}: n={len(v):4d}  median {st.median(v):.2f}  p95 {percentile(v, 95):.2f}  "
            f"p99 {percentile(v, 99):.2f}  max {max(v):.2f}")


def report_stamps(snap: Snapshot, out: Report) -> None:
    s = stamp_findings(snap)
    out("\n" + "=" * 72)
    out(f"Q2  STAMPS in {snap.name}: {s['rows']} unique rows (month windows {s['windows'][0]}..{s['windows'][-1]}, most of them empty)")
    out("    Share of stamps with seconds = 00, per kind of row (n = rows that have the field):")
    for group in ["booked", "completed", "no_show", "cancelled", "(soft-deleted)"]:
        rows = s["groups"].get(group, [])
        cells = []
        for field in STAMP_FIELDS:
            n, share = seconds_zero_share(rows, field)
            if n:
                cells.append(f"{field} {share:6.1%} (n={n})")
        out(f"   {group:15s} " + "  ".join(cells))
    cv = s["created_vs_updated"]
    out(f"    Never-touched `booked` rows ({s['booked_n']}): updated_at against created_at: "
        + fmt_counter(cv, ["equal", "upd_floor", "upd_later_<60s", "apart"]))
    out(f"    `cancelled` rows with updated_at == cancelled_at: {s['cancelled_equal']} of {s['cancelled_n']}")
    ahead = s["ahead"]
    out("    Stamps AHEAD of the server's Date header on the page that returned them: "
        + (", ".join(f"{k} {v}" for k, v in ahead.items()) if ahead else "none"))
    for field, (gap, text) in s["worst"].items():
        out(f"      worst {field}: {gap:.0f} s ({text})")
    if s["closest"] is not None:
        out(f"    Smallest gap between a page's Date and the newest `updated_at` it carried: {s['closest']:.0f} s")


def report_pair(p: Snapshot, q: Snapshot, margin: float, out: Report) -> None:
    windows = sorted(p.usable_windows() & q.usable_windows())
    # keep windows that hold data in either snapshot (the empty early months add nothing)
    windows = [w for w in windows if p.rows([w]) or q.rows([w])]
    if not windows:
        return
    f = pair_findings(p, q, windows, margin)
    gap = min(pg.server_time for pgs in q.pages.values() for pg in pgs) - max(pg.server_time for pgs in p.pages.values() for pg in pgs)
    out("\n" + "=" * 72)
    out(f"PAIR  {p.name}  ->  {q.name}")
    out(f"   {gap.total_seconds() / 60:.0f} min from the end of the earlier pull to the start of the later one")
    out(f"   windows compared: {windows}")
    for w in windows:
        a, b = p.window_times(w)
        out(f"   earlier snapshot read {w} between {a:%H:%M:%S} and {b:%H:%M:%S} (server time)")
    out(f"   rows earlier {f['rows_p']}  later {f['rows_q']}  new {f['new']}  gone {f['gone']}  changed {f['changed']}")
    order = ["after", "during", "just_before", "before"]
    out(f"Q1  new rows, created_at against the earlier read of their window:  {fmt_counter(f['new_created'], order)}")
    out(f"    new rows, updated_at against it:                                {fmt_counter(f['new_updated'], order)}")
    out(f"    changed rows, their new updated_at against it:                  {fmt_counter(f['changed_updated'], order)}")
    out(f"    (after = consistent; during = ambiguous; just_before = within {margin:.0f} s before the read; before = should have been visible)")
    if f["after_gap_min"] is not None:
        out(f"    closest a stamp comes after the end of the earlier read: {f['after_gap_min']:.0f} s")
    for line in f["examples"]:
        out(f"    ! {line}")
    out(f"Q5  rows whose updated_at went backwards: {f['backwards']}   changed with updated_at not bumped: {f['not_bumped']}   "
        f"un-deleted: {f['undeleted']}")
    out(f"    fields that changed: {dict(f['fields'])}")
    out(f"    changes outside status/updated_at/cancelled_at/deleted_at: {dict(f['other_fields']) or 'none'}")
    out(f"    status transitions: {dict(f['transitions']) or 'none'}")
    if f["gone"]:
        out(f"    ! {f['gone']} rows present earlier are absent later (hard deletes?)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet.explore.profile_watermark", description=__doc__.split("\n")[0])
    parser.add_argument("--root", default="data/raw")
    parser.add_argument("--margin", type=float, default=60.0, help="seconds before a read that still count as ambiguous")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"{root} not found.")
        return 2

    snapshots = [s for s in (load_snapshot(f) for f in sorted(root.glob("*_pull"))) if s]
    out = Report()
    out(f"Snapshots used: {[s.name for s in snapshots]}")
    report_clocks(root, out)
    for snap in snapshots:
        if len(snap.usable_windows()) >= 5:
            report_stamps(snap, out)
    for i, p in enumerate(snapshots):
        for q in snapshots[i + 1:]:
            report_pair(p, q, args.margin, out)
    path = root / "watermark_report.txt"
    path.write_text("\n".join(out.lines) + "\n")
    print(f"\nSaved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
