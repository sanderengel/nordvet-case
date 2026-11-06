"""Profile a saved `pull` snapshot (Phase 2). Offline: makes no API calls.

    python -m nordvet.explore.profile_raw                      # newest complete snapshot
    python -m nordvet.explore.profile_raw --folder data/raw/<stamp>_pull
    python -m nordvet.explore.profile_raw --compare data/raw/<older>_pull

Reads the raw pages that `explore_full pull` saved, merges them by appointment id
and prints a report, which is also saved as profile_report.txt inside the folder.
With --compare it diffs the folder against an older snapshot (what changed in between).

The report is descriptive. Where a number depends on a definition that is still
undecided (what counts as a no-show), it says so and shows the alternatives.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CPH = "Europe/Copenhagen"
PAGE_FILE = re.compile(r"pull_(normal|deleted)_(\d{4}-\d{2})_p\d+(?:_retry\d+)?\.json$")


# ----------------------------------------------------------------- pure helpers


def parse_sched(ts: str) -> tuple[datetime, timedelta, datetime]:
    """'2026-10-30T08:00+01:00' -> (local wall time, offset, UTC time)."""
    naive = datetime.strptime(ts[:16], "%Y-%m-%dT%H:%M")
    sign = 1 if ts[-6] == "+" else -1
    offset = sign * timedelta(hours=int(ts[-5:-3]), minutes=int(ts[-2:]))
    return naive, offset, naive - offset


def fmt_offset(td: timedelta) -> str:
    minutes = int(td.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def expected_offset(naive: datetime, tzname: str) -> str:
    """The UTC offset the clinic's timezone has at that local wall time."""
    return fmt_offset(naive.replace(tzinfo=ZoneInfo(tzname)).utcoffset())


def lead_bucket(hours: float) -> str:
    """How long before the appointment's start something happened (negative = after the start)."""
    if hours < 0:
        return "after the start"
    if hours < 2:
        return "under 2 h before"
    if hours < 24:
        return "2-24 h before"
    if hours < 168:
        return "1-7 days before"
    return "7+ days before"


BUCKET_ORDER = ["after the start", "under 2 h before", "2-24 h before", "1-7 days before", "7+ days before"]


def parse_z(ts: str) -> datetime:
    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")


# ----------------------------------------------------------------- loading


def load_pull(folder: Path) -> tuple[dict, list[dict]]:
    """Returns ({(variant, window): {id: row}}, pull_summary entries)."""
    windows: dict[tuple[str, str], dict[str, dict]] = {}
    for path in sorted(folder.glob("*.json")):
        m = PAGE_FILE.search(path.name)
        if not m:
            continue
        try:
            body = json.loads(path.read_bytes())
        except ValueError:
            continue
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            bucket = windows.setdefault((m.group(1), m.group(2)), {})
            for row in body["data"]:
                bucket[row["id"]] = row
    summary_path = folder / "pull_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else []
    return windows, summary


def merge(windows: dict, variant: str) -> tuple[dict[str, dict], int, int]:
    out: dict[str, dict] = {}
    dups = conflicts = 0
    for (v, _), rows in windows.items():
        if v != variant:
            continue
        for rid, row in rows.items():
            if rid in out:
                dups += 1
                conflicts += out[rid] != row
            else:
                out[rid] = row
    return out, dups, conflicts


def is_complete(summary: list[dict]) -> bool:
    return bool(summary) and not any(e.get("note") for e in summary)


def find_folder(root: Path) -> Path | None:
    folders = sorted(p for p in root.glob("*_pull") if p.is_dir())
    complete = [p for p in folders if is_complete(load_summary(p))]
    return (complete or folders or [None])[-1]


def load_summary(folder: Path) -> list[dict]:
    path = folder / "pull_summary.json"
    return json.loads(path.read_text()) if path.exists() else []


def load_clinics(root: Path) -> dict[str, dict]:
    files = [p for p in sorted(root.glob("*/*_clinics.json")) if not p.name.endswith(".meta.json")]
    for path in reversed(files):
        try:
            return {c["id"]: c for c in json.loads(path.read_bytes())["data"]}
        except (ValueError, KeyError):
            continue
    return {}


# ----------------------------------------------------------------- the report


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)


def profile(folder: Path, today, out: Report) -> dict:
    windows, summary = load_pull(folder)
    normal, dup_n, conf_n = merge(windows, "normal")
    deleted_variant, _, _ = merge(windows, "deleted")
    extras = {i: r for i, r in deleted_variant.items() if i not in normal}
    clinics = load_clinics(folder.parent)
    tz_of = {cid: c.get("timezone", CPH) for cid, c in clinics.items()}
    rows = list(normal.values())

    out(f"SNAPSHOT {folder.name}   (today = {today})")
    out("=" * 72)
    out("\n1. Completeness")
    if is_complete(summary):
        out("   every window finished without a STOPPED note")
    else:
        out("   WARNING: some windows stopped early, so counts below may be too low:")
        for e in summary:
            if e.get("note"):
                out(f"   {e['window']} {e['variant']}: {e['note']} (rows={e['rows']})")
    out(f"   windows overlap by a day: {dup_n} ids seen twice, {conf_n} with differing content")

    out("\n2. Volume")
    months = Counter(r["scheduled_at"][:7] for r in rows)
    out(f"   visible appointments: {len(normal)}   soft-deleted (only with include_deleted): {len(extras)}")
    out(f"   by month: {dict(sorted(months.items()))}")
    first = min(r["scheduled_at"] for r in rows)
    last = max(r["scheduled_at"] for r in rows)
    out(f"   scheduled from {first} to {last}")
    per_day = Counter(r["scheduled_at"][:10] for r in rows)
    weekday = defaultdict(list)
    for d, n in per_day.items():
        weekday[datetime.strptime(d, "%Y-%m-%d").strftime("%a")].append(n)
    out("   average rows per weekday: " + ", ".join(
        f"{d} {sum(v) // len(v)}" for d, v in sorted(weekday.items(), key=lambda kv: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].index(kv[0]))
    ) + ("" if "Sun" in weekday else ", Sun none"))

    def local_date(r: dict) -> str:
        return parse_sched(r["scheduled_at"])[0].strftime("%Y-%m-%d")

    past = [r for r in rows if local_date(r) < str(today)]
    ahead = [r for r in rows if local_date(r) >= str(today)]
    out("\n3. Statuses")
    out(f"   before today: {len(past)} {dict(Counter(r['status'] for r in past))}")
    out(f"   today and later: {len(ahead)} {dict(Counter(r['status'] for r in ahead))}")
    stale = [r for r in past if r["status"] == "booked"]
    out(f"   past but still 'booked': {len(stale)} by date {dict(sorted(Counter(local_date(r) for r in stale).items()))}")

    out("\n4. Cancellations")
    cancelled = [r for r in rows if r["status"] == "cancelled"]
    out(
        f"   cancelled rows: {len(cancelled)}; with cancelled_at: {sum(1 for r in cancelled if r.get('cancelled_at'))}; "
        f"cancelled_at on other statuses: {sum(1 for r in rows if r['status'] != 'cancelled' and r.get('cancelled_at'))}"
    )
    lead: dict[str, float] = {}
    for r in cancelled:
        if r.get("cancelled_at"):
            lead[r["id"]] = (parse_sched(r["scheduled_at"])[2] - parse_z(r["cancelled_at"])).total_seconds() / 3600
    buckets = Counter(lead_bucket(h) for h in lead.values())
    out("   when they were cancelled relative to the start: " + ", ".join(f"{b}: {buckets.get(b, 0)}" for b in BUCKET_ORDER))

    out("\n5. Soft-deleted rows")
    out(f"   statuses: {dict(Counter(r['status'] for r in extras.values()))}")
    out(f"   by month: {dict(sorted(Counter(r['scheduled_at'][:7] for r in extras.values()).items()))}")
    rel = Counter(
        "deleted_at after updated_at" if r["deleted_at"] > r["updated_at"]
        else "deleted_at equals updated_at" if r["deleted_at"] == r["updated_at"]
        else "deleted_at before updated_at"
        for r in extras.values()
    )
    before_start = sum(1 for r in extras.values() if parse_z(r["deleted_at"]) < parse_sched(r["scheduled_at"])[2])
    out(f"   deleted_at vs updated_at: {dict(rel)}")
    out(f"   deleted before the appointment's start: {before_start}; of those, status completed or no_show: "
        f"{sum(1 for r in extras.values() if parse_z(r['deleted_at']) < parse_sched(r['scheduled_at'])[2] and r['status'] in ('completed', 'no_show'))}")

    out("\n6. No-shows per clinic (rows before today; DEFINITION STILL OPEN)")
    out("   strict    = no_show / (completed + no_show)")
    out("   sensitivity = also counts 'cancelled' rows cancelled less than 2 h before the start or after it")
    per = defaultdict(lambda: Counter())
    for r in past:
        c = per[r["clinic_id"]]
        if r["status"] in ("completed", "no_show"):
            c[r["status"]] += 1
        elif r["status"] == "cancelled":
            c["cancelled"] += 1
            h = lead.get(r["id"])
            if h is not None and h < 2:
                c["late_cancel"] += 1
    table = []
    for cid, c in per.items():
        denom = c["completed"] + c["no_show"]
        strict = c["no_show"] / denom if denom else 0.0
        d2 = denom + c["late_cancel"]
        sens = (c["no_show"] + c["late_cancel"]) / d2 if d2 else 0.0
        table.append((strict, cid, c, sens))
    table.sort(reverse=True)
    out(f"   {'clinic':<8}{'name':<24}{'cty':<5}{'active':<8}{'compl':>7}{'no_show':>8}{'strict':>8}{'late_c':>7}{'sens.':>8}")
    for strict, cid, c, sens in table:
        meta = clinics.get(cid, {})
        out(f"   {cid:<8}{meta.get('name', '?')[:22]:<24}{meta.get('country', '?'):<5}{str(meta.get('active', '?')):<8}"
            f"{c['completed']:>7}{c['no_show']:>8}{strict:>8.1%}{c['late_cancel']:>7}{sens:>8.1%}")
    tot_c = sum(c["completed"] for _, _, c, _ in table)
    tot_n = sum(c["no_show"] for _, _, c, _ in table)
    out(f"   overall strict rate: {tot_n / (tot_c + tot_n):.2%}  ({tot_n} of {tot_c + tot_n})")

    out("\n7. Data quality")
    out(f"   field sets: {dict(Counter(len(r) for r in rows))} keys per row; missing/empty vet_id: "
        f"{sum(1 for r in rows if not r.get('vet_id'))}, patient_ref: {sum(1 for r in rows if not r.get('patient_ref'))}")
    orphans = sorted({r['clinic_id'] for r in rows} - set(clinics)) if clinics else "(no clinics file found)"
    out(f"   clinic ids not in /v1/clinics: {orphans}")
    out(f"   service -> duration: {dict(sorted({(r['service'], r['duration_minutes']) for r in rows}))}")
    bad_off = sum(
        1 for r in rows
        if r["scheduled_at"][-6:] != expected_offset(parse_sched(r["scheduled_at"])[0], tz_of.get(r["clinic_id"], CPH))
    )
    out(f"   offsets differing from the clinic timezone's rule: {bad_off} of {len(rows)}; used: {dict(Counter(r['scheduled_at'][-6:] for r in rows))}")
    out(f"   updated_at before created_at: {sum(1 for r in rows if r['updated_at'] < r['created_at'])}; "
        f"created after the start: {sum(1 for r in rows if parse_z(r['created_at']) > parse_sched(r['scheduled_at'])[2])}")
    vets = defaultdict(set)
    for r in rows:
        vets[r["vet_id"]].add(r["clinic_id"])
    out(f"   vets: {len(vets)}; working at more than one clinic: {sum(1 for v in vets.values() if len(v) > 1)}")
    patients = Counter(r["patient_ref"] for r in rows)
    ns_patients = Counter(r["patient_ref"] for r in rows if r["status"] == "no_show")
    out(f"   patients: {len(patients)}; with 2+ appointments: {sum(1 for n in patients.values() if n > 1)}; "
        f"with 2+ no-shows: {sum(1 for n in ns_patients.values() if n > 1)}; max appointments for one patient: {max(patients.values())}")
    clash = Counter((r["patient_ref"], r["scheduled_at"]) for r in rows)
    out(f"   same patient at the same time under different ids: {sum(1 for v in clash.values() if v > 1)} pairs")
    inactive = [cid for cid, c in clinics.items() if not c.get("active", True)]
    for cid in inactive:
        sub = [r["scheduled_at"] for r in rows if r["clinic_id"] == cid]
        out(f"   inactive clinic {cid}: {len(sub)} rows, {min(sub) if sub else '-'} .. {max(sub) if sub else '-'}")
    return {"normal": normal, "deleted_variant": deleted_variant, "summary": summary, "windows": windows}


def compare(newer: dict, older: dict, out: Report) -> None:
    out("\n" + "=" * 72)
    out("COMPARISON with the older snapshot")
    ok_new = {(e["window"], e["variant"]) for e in newer["summary"] if not e.get("note")}
    ok_old = {(e["window"], e["variant"]) for e in older["summary"] if not e.get("note")}
    shared = sorted(w for w, v in ok_new & ok_old if v == "normal" and (w, "deleted") in ok_new & ok_old)
    out(f"   windows complete in both (normal and deleted variants): {shared}")
    a = {i: r for (v, w), rows in older["windows"].items() if v == "normal" and w in shared for i, r in rows.items()}
    b = {i: r for (v, w), rows in newer["windows"].items() if v == "normal" and w in shared for i, r in rows.items()}
    b_del = {i for (v, w), rows in newer["windows"].items() if v == "deleted" and w in shared for i in rows}
    new_ids, gone = set(b) - set(a), set(a) - set(b)
    out(f"   rows in older: {len(a)}   in newer: {len(b)}   new: {len(new_ids)}   gone from the normal view: {len(gone)} "
        f"(of which now visible with include_deleted: {len(gone & b_del)})")
    changed = [(i, a[i], b[i]) for i in set(a) & set(b) if a[i] != b[i]]
    out(f"   rows whose content changed: {len(changed)}")
    if changed:
        fields = Counter(k for _, x, y in changed for k in set(x) | set(y) if x.get(k) != y.get(k))
        out(f"   fields that changed: {dict(fields)}")
        out(f"   status transitions: {dict(Counter((x['status'], y['status']) for _, x, y in changed if x['status'] != y['status']))}")
    out(f"   new rows by status: {dict(Counter(b[i]['status'] for i in new_ids))}; created_at range: "
        f"{min((b[i]['created_at'] for i in new_ids), default='-')} .. {max((b[i]['created_at'] for i in new_ids), default='-')}")


# ----------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet.explore.profile_raw", description=__doc__.split("\n")[0])
    parser.add_argument("--folder", help="a *_pull folder (default: newest complete one in data/raw)")
    parser.add_argument("--compare", help="an older *_pull folder to diff against")
    parser.add_argument("--today", help="YYYY-MM-DD, local date (default: the folder's own date)")
    parser.add_argument("--root", default="data/raw")
    args = parser.parse_args(argv)

    folder = Path(args.folder) if args.folder else find_folder(Path(args.root))
    if folder is None or not folder.is_dir():
        print("No *_pull folder found. Run `python -m nordvet.explore.explore_full pull` first.")
        return 2
    if args.today:
        today = datetime.strptime(args.today, "%Y-%m-%d").date()
    else:
        stamp = datetime.strptime(folder.name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        today = stamp.astimezone(ZoneInfo(CPH)).date()

    out = Report()
    newer = profile(folder, today, out)
    if args.compare:
        windows, summary = load_pull(Path(args.compare))
        compare(newer, {"windows": windows, "summary": summary}, out)
    report_path = folder / "profile_report.txt"
    report_path.write_text("\n".join(out.lines) + "\n")
    print(f"\nSaved to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
