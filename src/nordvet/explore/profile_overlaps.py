"""Test whether vets' appointments overlap by chance (Phase 2). Offline: makes no API calls.

    python -m nordvet.explore.profile_overlaps                      # newest complete snapshot
    python -m nordvet.explore.profile_overlaps --folder data/raw/<stamp>_pull
    python -m nordvet.explore.profile_overlaps --runs 50 --seed 1

Reads the raw pages that `explore_full pull` saved (same loader as profile_raw) and
prints six checks, also saved as overlap_report.txt inside the folder:

1. Concurrency: how many appointments one vet has at the same time ("n-booking").
2. Free or taken: at the moment a booking is made, does it land on time the vet
   already has booked more or less often than a random time in the same vet's day?
3. Service mix: do overlaps favour quick services (where parallel work is plausible),
   compared with random placement adjusted for duration?
4. Concentration: are overlaps concentrated on some clinics or vets, beyond what a
   vet's load explains? Baseline: shuffle which vet each appointment belongs to
   within each clinic and day.
5. Refills: is a cancelled appointment's time booked again more often than chance?
6. Outcomes by n: do no-shows, cancellations after the start or late completion vary
   with n once clinic and service are accounted for? (Traces real overbooking could leave.)

Scope: soft-deleted rows are excluded (D-015). An appointment occupies
[scheduled_at, scheduled_at + duration_minutes). "Live" means not cancelled, or
cancelled but only after the moment being looked at. Randomness is seeded, so the
report is reproducible.
"""

from __future__ import annotations

import argparse
import random
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from nordvet.explore.profile_raw import Report, find_folder, load_pull, merge, parse_sched, parse_z

GRID = timedelta(minutes=15)


# ----------------------------------------------------------------- pure helpers


def prepare(rows: dict[str, dict]) -> list[dict]:
    """Add parsed times: _local (local start), _s/_e (UTC start/end), _c (created), _x (cancelled or None)."""
    out = []
    for r in rows.values():
        local, _, start = parse_sched(r["scheduled_at"])
        r = dict(r)
        r["_local"] = local
        r["_s"] = start
        r["_e"] = start + timedelta(minutes=r["duration_minutes"])
        r["_c"] = parse_z(r["created_at"])
        r["_x"] = parse_z(r["cancelled_at"]) if r.get("cancelled_at") else None
        out.append(r)
    return out


def peak_concurrency(group: list[dict]) -> dict[str, int]:
    """For each appointment in one vet's group: the most appointments running at once during it (itself included).

    Intervals are half-open, so one ending at 10:00 and one starting at 10:00 do not overlap.
    """
    events = []
    for r in group:
        events.append((r["_s"], 1, r["id"]))
        events.append((r["_e"], -1, r["id"]))
    events.sort(key=lambda e: (e[0], e[1]))  # ends before starts at the same instant
    active: set[str] = set()
    peak: dict[str, int] = {}
    for _, step, rid in events:
        if step == 1:
            active.add(rid)
            for a in active:
                peak[a] = max(peak.get(a, 0), len(active))
        else:
            active.discard(rid)
    return peak


def minutes_at_level(group: list[dict]) -> Counter:
    """Booked minutes in one vet's group, by how many appointments run at once."""
    events = sorted(
        [(r["_s"], 1) for r in group] + [(r["_e"], -1) for r in group], key=lambda e: (e[0], e[1])
    )
    out: Counter = Counter()
    level, prev = 0, None
    for t, step in events:
        if prev is not None and level > 0:
            out[level] += (t - prev).total_seconds() / 60
        level += step
        prev = t
    return out


def is_taken(group: list[dict], start: datetime, end: datetime, at: datetime, exclude: str) -> bool:
    """Was [start, end) already booked for this vet at moment `at`?

    Counts appointments created before `at` and not cancelled by then.
    """
    for o in group:
        if o["id"] == exclude or o["_c"] >= at:
            continue
        if o["_x"] is not None and o["_x"] <= at:
            continue
        if o["_s"] < end and start < o["_e"]:
            return True
    return False


def overlap_flags(groups: dict) -> dict[str, bool]:
    """True for every appointment that overlaps at least one other in its group."""
    flag: dict[str, bool] = {}
    for group in groups.values():
        g = sorted(group, key=lambda r: r["_s"])
        for r in g:
            flag.setdefault(r["id"], False)
        for i, a in enumerate(g):
            for b in g[i + 1:]:
                if b["_s"] >= a["_e"]:
                    break
                flag[a["id"]] = flag[b["id"]] = True
    return flag


def group_by(rows: list[dict], vet_of: dict[str, str] | None = None) -> dict:
    """Group by (vet, local date); vet_of overrides the vet (used by the shuffle)."""
    groups: dict = defaultdict(list)
    for r in rows:
        vet = vet_of[r["id"]] if vet_of else r["vet_id"]
        groups[(vet, r["_local"].date())].append(r)
    return groups


def expected_pairs(count: Counter, duration: dict[str, int], total_pairs: int) -> dict[tuple, float]:
    """Overlapping pairs per service combination if placement were random.

    Two random intervals of lengths a and b overlap with probability proportional to a + b.
    """
    weight = {}
    names = sorted(count)
    for i, x in enumerate(names):
        for y in names[i:]:
            w = count[x] * (count[y] - (x == y))
            w = w / 2 if x == y else w
            weight[(x, y)] = w * (duration[x] + duration[y])
    total = sum(weight.values())
    return {k: total_pairs * w / total for k, w in weight.items()}


def observed_vs_expected(records) -> dict:
    """records: iterable of (stratum, bucket, value). Per bucket: (count, observed mean, expected mean).

    Expected averages each record's stratum mean, so it adjusts for how the strata
    (here clinic and service) are mixed in each bucket.
    """
    records = list(records)
    s_sum, s_n = Counter(), Counter()
    for stratum, _, value in records:
        s_sum[stratum] += value
        s_n[stratum] += 1
    mean = {k: s_sum[k] / s_n[k] for k in s_n}
    agg: dict = defaultdict(lambda: [0, 0.0, 0.0])
    for stratum, bucket, value in records:
        a = agg[bucket]
        a[0] += 1
        a[1] += value
        a[2] += mean[stratum]
    return {b: (n, total / n, exp / n) for b, (n, total, exp) in agg.items()}


# ----------------------------------------------------------------- the checks


def check_concurrency(live: list[dict], out: Report) -> None:
    for label, keep in (
        ("non-cancelled (completed, no_show, booked)", lambda r: True),
        ("completed only (both appointments happened)", lambda r: r["status"] == "completed"),
    ):
        groups = defaultdict(list)
        for r in live:
            if keep(r):
                groups[r["vet_id"]].append(r)
        n_per: Counter = Counter()
        minutes: Counter = Counter()
        example = None
        for vet, g in groups.items():
            peak = peak_concurrency(g)
            n_per.update(peak.values())
            minutes.update(minutes_at_level(g))
            top = max(peak.values())
            if example is None or top > example[0]:
                rid = max(peak, key=peak.get)
                hit = next(r for r in g if r["id"] == rid)
                example = (top, vet, hit["scheduled_at"])
        total, total_min = sum(n_per.values()), sum(minutes.values())
        out(f"   {label}: {total} appointments, max n = {example[0]} (e.g. {example[1]} around {example[2]})")
        out(f"     {'n':>3} {'appointments':>13} {'share':>7} {'booked vet-minutes':>19}")
        for n in sorted(n_per):
            out(f"     {n:>3} {n_per[n]:>13} {100 * n_per[n] / total:6.1f}% {100 * minutes[n] / total_min:18.1f}%")


def check_free_or_taken(rows: list[dict], rng: random.Random, out: Report) -> None:
    """Every appointment, taken-at-booking versus a random grid time in the same vet's day."""
    starts_by_weekday: dict[int, list] = defaultdict(list)
    for r in rows:
        starts_by_weekday[r["_local"].weekday()].append(r["_local"].time())
    window = {d: (min(v), max(v)) for d, v in starts_by_weekday.items()}
    groups = group_by(rows)
    hit = base = n = 0
    for (_, day), g in groups.items():
        first, last = window[day.weekday()]
        lo = datetime.combine(day, first)
        slots = int((datetime.combine(day, last) - lo) / GRID) + 1
        for b in g:
            utc_minus_local = b["_s"] - b["_local"]
            n += 1
            hit += is_taken(g, b["_s"], b["_e"], b["_c"], b["id"])
            start = lo + GRID * rng.randrange(slots) + utc_minus_local
            base += is_taken(g, start, start + (b["_e"] - b["_s"]), b["_c"], b["id"])
    out(f"   {n} appointments (cancelled ones included, as they were live when booked).")
    out(f"   Landed on time the vet already had booked: {100 * hit / n:.1f}%")
    out(f"   Random time of the same length in that vet's day, same moment: {100 * base / n:.1f}%")
    out("   Window per weekday from the earliest to the latest start seen in the data, on the 15-minute grid.")


def check_service_mix(live: list[dict], out: Report) -> None:
    groups = defaultdict(list)
    for r in live:
        groups[r["vet_id"]].append(r)
    pairs: Counter = Counter()
    for g in groups.values():
        g = sorted(g, key=lambda r: r["_s"])
        for i, a in enumerate(g):
            for b in g[i + 1:]:
                if b["_s"] >= a["_e"]:
                    break
                pairs[tuple(sorted((a["service"], b["service"])))] += 1
    count = Counter(r["service"] for r in live)
    duration = {s: Counter(r["duration_minutes"] for r in live if r["service"] == s).most_common(1)[0][0] for s in count}
    expected = expected_pairs(count, duration, sum(pairs.values()))
    out(f"   {'pair':>28} {'observed':>9} {'expected':>9} {'ratio':>6}")
    for key in sorted(expected, key=lambda k: -pairs[k]):
        e = expected[key]
        out(f"   {' + '.join(key):>28} {pairs[key]:>9} {e:>9.0f} {pairs[key] / e:6.2f}")
    out("   Ratio near 1 = as random placement would give. Parallel work on quick services would show ratios above 1 there.")


def check_concentration(live: list[dict], runs: int, rng: random.Random, out: Report) -> None:
    vet_of = {r["id"]: r["vet_id"] for r in live}
    observed = overlap_flags(group_by(live))
    by_clinic_day = defaultdict(list)
    for r in live:
        by_clinic_day[(r["clinic_id"], r["_local"].date())].append(r["id"])
    sims = []
    for _ in range(runs):
        shuffled = {}
        for ids in by_clinic_day.values():
            vets = [vet_of[i] for i in ids]
            rng.shuffle(vets)
            shuffled.update(zip(ids, vets))
        sims.append((overlap_flags(group_by(live, shuffled)), shuffled))

    def rates(flag, assign, key):
        n, o = Counter(), Counter()
        for r in live:
            k = r["clinic_id"] if key == "clinic" else assign[r["id"]]
            n[k] += 1
            o[k] += flag[r["id"]]
        return {k: o[k] / n[k] for k in n}, n

    sim_share = st.mean(sum(f.values()) / len(f) for f, _ in sims)
    out(f"   Overlapping appointments overall: observed {100 * sum(observed.values()) / len(observed):.1f}%, "
        f"shuffled {100 * sim_share:.1f}% (mean of {runs} runs)")
    for key in ("clinic", "vet"):
        obs, n = rates(observed, vet_of, key)
        sim = [rates(f, a, key)[0] for f, a in sims]
        exp = {k: st.mean(s[k] for s in sim) for k in obs}
        sd_obs = st.pstdev([obs[k] - exp[k] for k in obs])
        sd_sim = st.mean(st.pstdev([s[k] - exp[k] for k in obs]) for s in sim)
        out(f"   By {key} ({len(obs)}): raw rates {100 * min(obs.values()):.1f}-{100 * max(obs.values()):.1f}%; "
            f"spread of observed minus expected sd {100 * sd_obs:.2f} pts, under the shuffle sd {100 * sd_sim:.2f} pts")
        ranked = sorted(obs, key=lambda k: obs[k] - exp[k])
        for k in ranked[:2] + ranked[-2:]:
            out(f"     {k}: {n[k]} appts, observed {100 * obs[k]:.1f}%, expected {100 * exp[k]:.1f}%")
    out("   Similar spreads = no concentration beyond what each vet's load explains.")


def check_refills(rows: list[dict], now: datetime, out: Report) -> None:
    buckets = [(1, "under 1 h"), (2, "1-2 h"), (4, "2-4 h"), (8, "4-8 h"), (24, "8-24 h"),
               (72, "1-3 days"), (168, "3-7 days"), (float("inf"), "7+ days")]

    def bucket(hours):
        return next(name for limit, name in buckets if hours < limit)

    by_vet = defaultdict(list)
    for r in rows:
        by_vet[r["vet_id"]].append(r)

    def overlapping(vet, start, end, exclude):
        return [o for o in by_vet[vet] if o["id"] != exclude and o["_s"] < end and start < o["_e"]]

    stats = defaultdict(lambda: [0, 0, 0])  # cancellations, refilled, chance hit
    for r in rows:
        if r["status"] != "cancelled" or r["_x"] is None or r["_e"] > now:
            continue
        notice = (r["_s"] - r["_x"]).total_seconds() / 3600
        if notice < 0:
            continue
        s = stats[bucket(notice)]
        s[0] += 1
        s[1] += any(o["_c"] >= r["_x"] for o in overlapping(r["vet_id"], r["_s"], r["_e"], r["id"]))
        week = timedelta(days=7)
        s[2] += any(
            o["status"] != "cancelled" and (o["_s"] - o["_c"]).total_seconds() / 3600 < notice
            for o in overlapping(r["vet_id"], r["_s"] - week, r["_e"] - week, r["id"])
        )
    out(f"   {'notice':>10} {'n':>6} {'refilled':>9} {'chance':>7}")
    for _, name in buckets:
        n, refilled, chance = stats[name]
        if n:
            out(f"   {name:>10} {n:>6} {100 * refilled / n:8.1f}% {100 * chance / n:6.1f}%")
    out("   Refilled: another appointment for the same vet overlaps it and was created after the cancellation.")
    out("   Chance: same vet and time a week earlier holds a non-cancelled appointment booked with less lead")
    out("   than this cancellation's notice. Refilled close to chance = cancelling does not free anything.")


def check_outcomes(rows: list[dict], live: list[dict], out: Report) -> None:
    by_vet = defaultdict(list)
    for r in live:
        by_vet[r["vet_id"]].append(r)
    peak: dict[str, int] = {}
    for g in by_vet.values():
        peak.update(peak_concurrency(g))
    after_start = [r for r in rows if r["status"] == "cancelled" and r["_x"] is not None and r["_x"] >= r["_s"]]
    for r in after_start:  # n for a cancelled row: itself plus the live appointments around it
        group = [o for o in by_vet[r["vet_id"]] if o["_s"] < r["_e"] and r["_s"] < o["_e"]] + [r]
        peak[r["id"]] = peak_concurrency(group)[r["id"]]
    labels = {1: "1", 2: "2", 3: "3", 4: "4+"}

    def bucket(r):
        return min(peak[r["id"]], 4)

    def stratum(r):
        return (r["clinic_id"], r["service"])

    def table(title, result, fmt):
        out(f"   {title}")
        out(f"     {'n':>3} {'appts':>7} {'observed':>9} {'expected':>9}")
        for b in sorted(result):
            n, observed, expected = result[b]
            out(f"     {labels[b]:>3} {n:>7} {fmt(observed):>9} {fmt(expected):>9}")

    def pct(x):
        return f"{100 * x:.2f}%"

    done = [r for r in live if r["status"] in ("completed", "no_show")]
    table("No-show rate (of completed + no_show):",
          observed_vs_expected((stratum(r), bucket(r), r["status"] == "no_show") for r in done), pct)
    ids = {r["id"] for r in after_start}
    table("Cancelled after the start (of completed + no_show + cancelled after the start):",
          observed_vs_expected((stratum(r), bucket(r), r["id"] in ids) for r in done + after_start), pct)
    completed = [r for r in live if r["status"] == "completed"]
    lag = {r["id"]: (parse_z(r["updated_at"]) - r["_e"]).total_seconds() / 60 for r in completed}
    table("Minutes from scheduled end to updated_at on completed rows (mean):",
          observed_vs_expected((stratum(r), bucket(r), lag[r["id"]]) for r in completed), lambda x: f"{x:.0f}")
    medians = defaultdict(list)
    for r in completed:
        medians[bucket(r)].append(lag[r["id"]])
    out("     median minutes by n: " + ", ".join(f"{labels[b]}: {st.median(v):.0f}" for b, v in sorted(medians.items())))
    out("   Expected = each appointment's clinic-and-service mean, averaged (adjusts for clinic and service mix).")
    out("   Limits (D-019): no-shows cannot reveal overbooking, since clients cannot know in advance; updated_at looks")
    out("   like when staff record the outcome, so the lag barely measures delays.")


# ----------------------------------------------------------------- main


def run(folder: Path, runs: int, seed: int, out: Report) -> None:
    windows, _ = load_pull(folder)
    normal, _, _ = merge(windows, "normal")
    deleted, _, _ = merge(windows, "deleted")
    deleted_ids = {i for i, r in deleted.items() if r.get("deleted_at")}
    rows = prepare({i: r for i, r in normal.items() if i not in deleted_ids})
    live = [r for r in rows if r["status"] != "cancelled"]
    now = datetime.strptime(folder.name[:15], "%Y%m%dT%H%M%S")  # snapshot time, UTC (naive like the rest)
    rng = random.Random(seed)

    out(f"OVERLAP REPORT {folder.name}   ({len(rows)} appointments, {len(deleted_ids)} soft-deleted left out, seed {seed})")
    out("\n1. Concurrency per vet (n = most appointments at once during an appointment, itself included)")
    check_concurrency(live, out)
    out("\n2. At the moment of booking: free or already taken?")
    check_free_or_taken(rows, rng, out)
    out("\n3. Overlapping pairs by service, against random placement adjusted for duration")
    check_service_mix(live, out)
    out("\n4. Concentration on clinics or vets (shuffle vets within clinic and day)")
    check_concentration(live, runs, rng, out)
    out("\n5. Is a cancelled appointment's time booked again more often than chance? (ended before the snapshot)")
    check_refills(rows, now, out)
    out("\n6. Do outcomes vary with n once clinic and service are accounted for?")
    check_outcomes(rows, live, out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet.explore.profile_overlaps", description=__doc__.split("\n")[0])
    parser.add_argument("--folder", help="a *_pull folder (default: newest complete one in data/raw)")
    parser.add_argument("--root", default="data/raw")
    parser.add_argument("--runs", type=int, default=20, help="shuffles for check 4 (default 20)")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    folder = Path(args.folder) if args.folder else find_folder(Path(args.root))
    if folder is None or not folder.is_dir():
        print("No *_pull folder found. Run `python -m nordvet.explore.explore_full pull` first.")
        return 2
    out = Report()
    run(folder, args.runs, args.seed, out)
    path = folder / "overlap_report.txt"
    path.write_text("\n".join(out.lines) + "\n")
    print(f"\nSaved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
