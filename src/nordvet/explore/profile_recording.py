"""When are statuses recorded? (Phase 2). Offline: makes no API calls.

    python -m nordvet.explore.profile_recording                      # newest complete snapshot
    python -m nordvet.explore.profile_recording --folder data/raw/<stamp>_pull

Reads the raw pages that `explore_full pull` saved (same loader as profile_raw) and
prints four checks, also saved as recording_report.txt inside the folder. Times are in
Copenhagen time (Danish and Swedish clinics follow the same clock).

1. no_show: how many days after the appointment, at what hour and on which weekday it
   is recorded, and whether it comes in batches.
2. completed: how much is recorded on the appointment's own day, and at what hours.
3. Confirmed against assumed daily rate for recent days (D-021): what a view shows when
   past appointments still `booked` are counted as no-shows, and how often an
   appointment still `booked` after its day turns out `completed` (the overshoot).
4. Cancellations recorded after the start (D-022): when they are entered, and whether
   they look like ordinary cancellations, disguised no-shows or clinic-side cancellations.

Scope: soft-deleted rows are excluded (D-015). `updated_at` is taken as the moment the
current status was set; for cancelled rows it equals `cancelled_at`. Randomness (the
baseline in check 4) is seeded, so the report is reproducible.
"""

from __future__ import annotations

import argparse
import random
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nordvet.explore.profile_overlaps import prepare
from nordvet.explore.profile_raw import CPH, Report, find_folder, load_pull, merge, parse_z

TZ = ZoneInfo(CPH)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ----------------------------------------------------------------- pure helpers


def to_local(ts: str) -> datetime:
    """'2026-10-01T06:55:00Z' -> Copenhagen wall time (naive)."""
    return parse_z(ts).replace(tzinfo=timezone.utc).astimezone(TZ).replace(tzinfo=None)


def day_counts(rows: list[dict], before: date) -> dict[date, tuple[int, int, int]]:
    """Per local scheduled date before `before`: (completed, no_show, still booked)."""
    slot = {"completed": 0, "no_show": 1, "booked": 2}
    out: dict = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        d = r["_local"].date()
        if d < before and r["status"] in slot:
            out[d][slot[r["status"]]] += 1
    return {d: tuple(v) for d, v in out.items()}


def confirmed_and_assumed(completed: int, no_show: int, booked: int) -> tuple[float, float]:
    """Confirmed rate (D-020) and assumed rate (D-021: still-booked past appointments count as no-shows)."""
    attended_or_missed = completed + no_show
    confirmed = no_show / attended_or_missed if attended_or_missed else 0.0
    total = attended_or_missed + booked
    assumed = (no_show + booked) / total if total else 0.0
    return confirmed, assumed


def group_size_counts(keys) -> Counter:
    """For a list of group keys: how many groups have k members."""
    return Counter(Counter(keys).values())


def corr(a: list[float], b: list[float]) -> float:
    """Pearson correlation."""
    ma, mb = st.mean(a), st.mean(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) * st.pstdev(a) * st.pstdev(b))


def quantiles(values: list[float], ps=(0.05, 0.25, 0.5, 0.75, 0.95)) -> list[float]:
    v = sorted(values)
    return [v[int(p * (len(v) - 1))] for p in ps]


# ----------------------------------------------------------------- the checks


def check_no_show(rows: list[dict], out: Report) -> None:
    ns = [r for r in rows if r["status"] == "no_show"]
    rec = {r["id"]: to_local(r["updated_at"]) for r in ns}
    days = Counter((rec[r["id"]].date() - r["_local"].date()).days for r in ns)
    out(f"   {len(ns)} no-shows. Days after the appointment's date: "
        + ", ".join(f"{k}: {v} ({v / len(ns):.0%})" for k, v in sorted(days.items())))
    hours = Counter(t.hour for t in rec.values())
    out("   hour recorded: " + ", ".join(f"{h:02d}: {hours[h]}" for h in range(24) if hours[h]))
    weekday = Counter(t.weekday() for t in rec.values())
    out("   weekday recorded: " + ", ".join(f"{WEEKDAYS[i]} {weekday[i]}" for i in range(7)))
    same = Counter(r["updated_at"] for r in ns)
    out(f"   batches? most no-shows sharing one exact timestamp: {max(same.values())}")
    hours_per_clinic_day: dict = defaultdict(set)
    n_per_clinic_day: Counter = Counter()
    for r in ns:
        key = (r["clinic_id"], r["_local"].date())
        hours_per_clinic_day[key].add(rec[r["id"]].replace(minute=0))
        n_per_clinic_day[key] += 1
    big = [k for k, n in n_per_clinic_day.items() if n >= 3]
    one_hour = sum(1 for k in big if len(hours_per_clinic_day[k]) == 1)
    out(f"   clinic-days with 3+ no-shows all recorded in the same clock hour: {one_hour} of {len(big)}")
    next_day, total = Counter(), Counter()
    for r in ns:
        total[r["clinic_id"]] += 1
        next_day[r["clinic_id"]] += (rec[r["id"]].date() - r["_local"].date()).days == 1
    shares = sorted(next_day[c] / total[c] for c in total)
    out(f"   share recorded the next day, per clinic: min {shares[0]:.0%}, "
        f"median {shares[len(shares) // 2]:.0%}, max {shares[-1]:.0%}")


def check_completed(rows: list[dict], out: Report) -> None:
    comp = [r for r in rows if r["status"] == "completed"]
    rec = {r["id"]: to_local(r["updated_at"]) for r in comp}
    days = Counter((rec[r["id"]].date() - r["_local"].date()).days for r in comp)
    out(f"   {len(comp)} completed. Recorded on the appointment's own day: {days[0]} ({days[0] / len(comp):.1%}); "
        "later: " + ", ".join(f"{k} d: {v}" for k, v in sorted(days.items()) if k))
    hours = Counter(t.hour for t in rec.values())
    out("   hour recorded: " + ", ".join(f"{h:02d}: {hours[h]}" for h in range(24) if hours[h]))
    lag = [(parse_z(r["updated_at"]) - r["_e"]).total_seconds() / 60
           for r in comp if rec[r["id"]].date() == r["_local"].date()]
    q = quantiles(lag)
    out(f"   same-day ones, minutes after the scheduled end: 5th {q[0]:.0f}, 25th {q[1]:.0f}, "
        f"median {q[2]:.0f}, 75th {q[3]:.0f}, 95th {q[4]:.0f}")


def check_daily(rows: list[dict], today: date, out: Report, last: int = 10) -> None:
    counts = day_counts(rows, today)
    out(f"   {'date':<12}{'completed':>10}{'no_show':>9}{'booked':>8}{'confirmed':>11}{'assumed':>9}")
    for d in sorted(counts)[-last:]:
        c, n, b = counts[d]
        confirmed, assumed = confirmed_and_assumed(c, n, b)
        out(f"   {d.isoformat():<12}{c:>10}{n:>9}{b:>8}{confirmed:>11.1%}{assumed:>9.1%}")
    past = [r for r in rows if r["_local"].date() < today]
    late = sum(1 for r in past if r["status"] == "completed" and to_local(r["updated_at"]).date() > r["_local"].date())
    no_show = sum(1 for r in past if r["status"] == "no_show")
    attended_or_missed = sum(1 for r in past if r["status"] in ("completed", "no_show"))
    out(f"   Of appointments still 'booked' after their day, {late} of {late + no_show} "
        f"({late / (late + no_show):.1%}) turned out completed rather than no-show.")
    out(f"   So the assumed rate overshoots by about {100 * late / attended_or_missed:.1f} percentage points on average.")
    out("   Read as of the snapshot time; a view before 08:00 would have no confirmed no-shows for yesterday.")


def check_after_start(rows: list[dict], today: date, rng: random.Random, out: Report, runs: int = 20) -> None:
    cancelled = [r for r in rows if r["status"] == "cancelled" and r["_x"] is not None]
    after = [r for r in cancelled if r["_x"] >= r["_s"]]
    before = [r for r in cancelled if r["_x"] < r["_s"]]
    lag = [(r["_x"] - r["_s"]).total_seconds() / 3600 for r in after]
    q = quantiles(lag)
    out(f"   {len(after)} of {len(cancelled)} cancellations recorded at or after the start.")
    out(f"   hours after the start: 5th {q[0]:.1f}, 25th {q[1]:.1f}, median {q[2]:.1f}, 75th {q[3]:.1f}, "
        f"95th {q[4]:.1f}, max {max(lag):.1f}")
    within = sum(1 for r in after if r["_x"] < r["_e"])
    days = Counter((to_local(r["cancelled_at"]).date() - r["_local"].date()).days for r in after)
    out(f"   recorded within the appointment's own duration: {within}; days after the appointment's date: "
        + ", ".join(f"{k}: {v}" for k, v in sorted(days.items())))
    for label, group in (("after the start", after), ("before the start", before)):
        hours = Counter(to_local(r["cancelled_at"]).hour for r in group)
        out(f"   hour recorded, {label}: " + ", ".join(f"{h:02d}: {hours[h]}" for h in range(24) if hours[h]))

    out("   Profile (booking lead time and service mix):")
    services = sorted({r["service"] for r in rows})
    groups = (
        ("cancelled after start", after),
        ("cancelled before start", before),
        ("no_show", [r for r in rows if r["status"] == "no_show"]),
        ("completed", [r for r in rows if r["status"] == "completed"]),
    )
    out(f"     {'group':<24}{'n':>7}{'median lead':>13}{'booked <24h':>13}  " + "  ".join(f"{s[:11]:>11}" for s in services))
    for label, group in groups:
        lead = [(r["_s"] - r["_c"]).total_seconds() / 86400 for r in group]
        mix = Counter(r["service"] for r in group)
        out(f"     {label:<24}{len(group):>7}{st.median(lead):>11.1f} d{sum(x < 1 for x in lead) / len(group):>13.1%}  "
            + "  ".join(f"{mix[s] / len(group):>11.0%}" for s in services))

    past = [r for r in rows if r["_local"].date() < today]
    after_ids = {r["id"] for r in after}
    per: dict = defaultdict(Counter)
    for r in past:
        per[r["clinic_id"]][r["status"]] += 1
        per[r["clinic_id"]]["after"] += r["id"] in after_ids
    xs, ys = [], []
    for counts in per.values():
        attended_or_missed = counts["completed"] + counts["no_show"]
        if attended_or_missed < 200:
            continue
        xs.append(counts["no_show"] / attended_or_missed)
        ys.append(counts["after"] / (attended_or_missed + counts["cancelled"]))
    out(f"   Per clinic, correlation of the no-show rate with the share cancelled after the start: {corr(xs, ys):.2f} "
        f"({len(xs)} clinics)")

    past_after = [r for r in past if r["id"] in after_ids]
    for name, key in (("vet-day", lambda r: (r["vet_id"], r["_local"].date())),
                      ("clinic-day", lambda r: (r["clinic_id"], r["_local"].date()))):
        observed = group_size_counts(key(r) for r in past_after)
        sims = [group_size_counts(key(r) for r in rng.sample(past, len(past_after))) for _ in range(runs)]
        top = max(max(observed), max(max(s) for s in sims))
        out(f"   Clustering per {name} (groups with k after-start cancellations, observed / random sample of the same size):")
        out("     " + ", ".join(f"k={k}: {observed.get(k, 0)} / {st.mean(s.get(k, 0) for s in sims):.1f}"
                                for k in range(1, top + 1)))
    out("   Disguised no-shows would look like no-shows (booked further ahead, rarely surgery, recorded")
    out("   a day later, more common where no-shows are common). Clinic-side cancellations (vet ill,")
    out("   emergency) would cluster on the same vet-day or clinic-day.")


# ----------------------------------------------------------------- main


def run(folder: Path, seed: int, out: Report) -> None:
    windows, _ = load_pull(folder)
    normal, _, _ = merge(windows, "normal")
    deleted, _, _ = merge(windows, "deleted")
    deleted_ids = {i for i, r in deleted.items() if r.get("deleted_at")}
    rows = prepare({i: r for i, r in normal.items() if i not in deleted_ids})
    stamp = datetime.strptime(folder.name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    today = stamp.astimezone(TZ).date()
    rng = random.Random(seed)

    out(f"RECORDING REPORT {folder.name}   (today = {today}, {len(rows)} appointments, "
        f"{len(deleted_ids)} soft-deleted left out, seed {seed})")
    out("\n1. When is a no-show recorded?")
    check_no_show(rows, out)
    out("\n2. When is a completed visit recorded?")
    check_completed(rows, out)
    out("\n3. Confirmed against assumed no-show rate for the last days (D-021)")
    check_daily(rows, today, out)
    out("\n4. Cancellations recorded after the start (D-022)")
    check_after_start(rows, today, rng, out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet.explore.profile_recording", description=__doc__.split("\n")[0])
    parser.add_argument("--folder", help="a *_pull folder (default: newest complete one in data/raw)")
    parser.add_argument("--root", default="data/raw")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    folder = Path(args.folder) if args.folder else find_folder(Path(args.root))
    if folder is None or not folder.is_dir():
        print("No *_pull folder found. Run `python -m nordvet.explore.explore_full pull` first.")
        return 2
    out = Report()
    run(folder, args.seed, out)
    path = folder / "recording_report.txt"
    path.write_text("\n".join(out.lines) + "\n")
    print(f"\nSaved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
