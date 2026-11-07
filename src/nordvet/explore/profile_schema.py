"""Schema profile of the saved snapshots (Phase 3.1, plan step 3). Offline: no API calls.

    PYTHONPATH=src python3 -m nordvet.explore.profile_schema [--root data/raw]

Answers Q14 and Q15: which fields exist and when, their JSON types, lengths, id formats,
non-ASCII text, whitespace oddities, and ids that differ only by case. Reads the all-rows
variant ("deleted") of every saved pull, merges by id (the newest pull wins) and also reads
the clinics response. Writes <root>/schema_report.txt.

The checks are descriptive: the id patterns below are what Phase 2 saw in sample rows, and
the report says how many rows really follow them and shows the ones that do not.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from nordvet.explore.profile_raw import Report, load_clinics, load_pull, merge

ID_PATTERNS = {
    "id": r"apt_[0-9a-f]{12}",
    "clinic_id": r"CL-\d{3}",
    "vet_id": r"V\d{4}",
    "patient_ref": r"P[0-9A-F]{8}",
}
TIMESTAMP_PATTERNS = {
    "scheduled_at": r"\d{4}-\d\d-\d\dT\d\d:\d\d[+-]\d\d:\d\d",
    "created_at": r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
    "updated_at": r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
    "cancelled_at": r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
    "deleted_at": r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
}
CLINIC_PATTERNS = {
    "id": r"CL-\d{3}",
    "opened_on": r"\d{4}-\d\d-\d\d",
    "country": r"[A-Z]{2}",
}


# ----------------------------------------------------------------- pure helpers


def type_name(value) -> str:
    """JSON type of a decoded value (bool is not int)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    return "object"


def shape(text: str) -> str:
    """'apt_03942c73' -> 'aaa_99999a99': digit 9, lower a, upper A, others as they are."""
    return re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", text)))


def non_ascii_chars(text: str) -> list[str]:
    return sorted({c for c in text if ord(c) > 127})


def conformance(values: list[str], pattern: str) -> tuple[int, list[str]]:
    """How many values match the pattern entirely, and up to five that do not."""
    rx = re.compile(pattern)
    bad = [v for v in values if not rx.fullmatch(v)]
    return len(values) - len(bad), bad[:5]


def case_collisions(values) -> list[list[str]]:
    """Groups of distinct values that are equal when case is ignored."""
    groups: dict[str, set[str]] = defaultdict(set)
    for v in values:
        groups[v.casefold()].add(v)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def key_sets(rows: list[dict], by: str) -> dict[str, Counter]:
    """For each value of field `by`, how often each exact set of keys occurs."""
    out: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        out[str(row.get(by))][tuple(sorted(row))] += 1
    return out


def field_profile(rows: list[dict]) -> dict[str, dict]:
    """Per field: rows with the key, JSON types, string length range, non-ASCII,
    whitespace at the ends, empty strings, and longest UTF-8 size."""
    prof: dict[str, dict] = {}
    for row in rows:
        for key, value in row.items():
            p = prof.setdefault(
                key,
                {"present": 0, "types": Counter(), "minlen": None, "maxlen": 0,
                 "maxbytes": 0, "non_ascii": 0, "padded": 0, "empty": 0},
            )
            p["present"] += 1
            p["types"][type_name(value)] += 1
            if isinstance(value, str):
                n = len(value)
                p["minlen"] = n if p["minlen"] is None else min(p["minlen"], n)
                p["maxlen"] = max(p["maxlen"], n)
                p["maxbytes"] = max(p["maxbytes"], len(value.encode("utf-8")))
                p["non_ascii"] += bool(non_ascii_chars(value))
                p["padded"] += value != value.strip()
                p["empty"] += value == ""
    return prof


# ----------------------------------------------------------------- loading


def load_all_rows(root: Path) -> tuple[dict[str, dict], list[str]]:
    """Union of the all-rows variant over every saved pull folder (newest pull wins)."""
    merged: dict[str, dict] = {}
    used: list[str] = []
    for folder in sorted(root.glob("*_pull")):
        windows, _ = load_pull(folder)
        rows, _, _ = merge(windows, "deleted")
        if rows:
            merged.update(rows)
            used.append(f"{folder.name} ({len(rows):,} ids)")
    return merged, used


# ----------------------------------------------------------------- reports


def report_fields(rows: list[dict], total: int, out: Report) -> None:
    out("\n1. Fields (all appointment rows; presence out of %s)" % f"{total:,}")
    prof = field_profile(rows)
    for key in sorted(prof, key=lambda k: (-prof[k]["present"], k)):
        p = prof[key]
        types = ", ".join(f"{t} {n:,}" for t, n in p["types"].most_common())
        line = f"   {key:<18} present {p['present']:>7,}   types: {types}"
        if p["maxlen"]:
            line += f"   len {p['minlen']}-{p['maxlen']} (max {p['maxbytes']} UTF-8 bytes)"
        extras = []
        if p["non_ascii"]:
            extras.append(f"non-ASCII {p['non_ascii']:,}")
        if p["padded"]:
            extras.append(f"padded with whitespace {p['padded']:,}")
        if p["empty"]:
            extras.append(f"empty {p['empty']:,}")
        if extras:
            line += "   " + ", ".join(extras)
        out(line)
    nulls = sum(p["types"].get("null", 0) for p in prof.values())
    out(f"   null values anywhere: {nulls:,}")


def report_key_sets(rows: list[dict], out: Report) -> None:
    out("\n2. Which keys go with which status (optional fields are absent, not null)")
    for status, sets in sorted(key_sets(rows, "status").items()):
        out(f"   status {status}:")
        for keys, n in sets.most_common():
            out(f"      {n:>7,} rows: " + ", ".join(keys))
    deleted_flag = Counter(("deleted_at" in r, r.get("status")) for r in rows)
    out("   deleted_at present by status: "
        + ", ".join(f"{s} {n:,}" for (d, s), n in sorted(deleted_flag.items(), key=str) if d))


def report_enums(rows: list[dict], out: Report) -> None:
    out("\n3. Values")
    for key in ("status", "service"):
        out(f"   {key}: " + ", ".join(f"{v} {n:,}" for v, n in Counter(r.get(key) for r in rows).most_common()))
    by_service: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_service[r.get("service")][(type_name(r.get("duration_minutes")), r.get("duration_minutes"))] += 1
    for service, c in sorted(by_service.items()):
        out(f"   duration_minutes for {service}: " + ", ".join(f"{t} {v} x{n:,}" for (t, v), n in c.most_common()))
    out(f"   distinct clinic_id {len({r['clinic_id'] for r in rows})}, vet_id {len({r['vet_id'] for r in rows})}, "
        f"patient_ref {len({r['patient_ref'] for r in rows})}")


def report_formats(rows: list[dict], out: Report) -> None:
    out("\n4. Formats (full-string pattern match; mismatches shown)")
    for field, pattern in {**ID_PATTERNS, **TIMESTAMP_PATTERNS}.items():
        values = [r[field] for r in rows if isinstance(r.get(field), str)]
        ok, bad = conformance(values, pattern)
        line = f"   {field:<13} {ok:>7,} of {len(values):>7,} match {pattern}"
        if bad:
            line += f"   e.g. {bad}"
        out(line)
        if bad:
            rx = re.compile(pattern)
            shapes = Counter(shape(v) for v in values if not rx.fullmatch(v))
            out("      shapes of non-matching: " + ", ".join(f"{s} x{n}" for s, n in shapes.most_common(5)))


def report_case_and_unicode(rows: list[dict], clinics: dict[str, dict], out: Report) -> None:
    out("\n5. Case-only collisions and non-ASCII text (Q15)")
    for field in ID_PATTERNS:
        values = {r[field] for r in rows if isinstance(r.get(field), str)}
        groups = case_collisions(values)
        out(f"   {field:<13} {len(values):>7,} distinct, groups differing only by case: {len(groups)}"
            + (f"   e.g. {groups[:3]}" if groups else ""))
    chars: Counter = Counter()
    fields: Counter = Counter()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, str) and non_ascii_chars(v):
                fields[k] += 1
                chars.update(non_ascii_chars(v))
    out("   appointment fields with non-ASCII text: " + (", ".join(f"{k} {n:,}" for k, n in fields.items()) or "none"))
    cl_chars: Counter = Counter()
    cl_fields: Counter = Counter()
    for c in clinics.values():
        for k, v in c.items():
            if isinstance(v, str) and non_ascii_chars(v):
                cl_fields[k] += 1
                cl_chars.update(non_ascii_chars(v))
    out("   clinic fields with non-ASCII text: "
        + (", ".join(f"{k} {n}" for k, n in cl_fields.items()) or "none")
        + ("   characters: " + " ".join(sorted(cl_chars)) if cl_chars else ""))


def report_clinics(rows: list[dict], clinics: dict[str, dict], out: Report) -> None:
    out("\n6. Clinics (/v1/clinics, one saved response)")
    if not clinics:
        out("   no clinics file found")
        return
    cl = list(clinics.values())
    prof = field_profile(cl)
    for key in sorted(prof, key=lambda k: (-prof[k]["present"], k)):
        p = prof[key]
        types = ", ".join(f"{t} {n}" for t, n in p["types"].most_common())
        extra = f"   len {p['minlen']}-{p['maxlen']} ({p['maxbytes']} bytes)" if p["maxlen"] else ""
        out(f"   {key:<12} present {p['present']:>3} of {len(cl)}   types: {types}{extra}")
    for field, pattern in CLINIC_PATTERNS.items():
        ok, bad = conformance([c[field] for c in cl if isinstance(c.get(field), str)], pattern)
        out(f"   {field:<12} {ok} of {len(cl)} match {pattern}" + (f"   e.g. {bad}" if bad else ""))
    out("   country: " + ", ".join(f"{v} {n}" for v, n in Counter(c.get("country") for c in cl).items())
        + "   timezone: " + ", ".join(f"{v} {n}" for v, n in Counter(c.get("timezone") for c in cl).items())
        + "   active: " + ", ".join(f"{v} {n}" for v, n in Counter(c.get("active") for c in cl).items()))
    used = {r["clinic_id"] for r in rows}
    out(f"   appointment clinic_ids missing from clinics: {sorted(used - set(clinics)) or 'none'};"
        f" clinics with no appointments: {sorted(set(clinics) - used) or 'none'}")


def envelope_keys(root: Path) -> tuple[Counter, Counter, int]:
    """Top-level keys and next_cursor types over every saved appointment page."""
    keys: Counter = Counter()
    cursors: Counter = Counter()
    pages = 0
    for folder in sorted(root.glob("*_pull")):
        for path in folder.glob("*_pull_*_p*.json"):
            if path.name.endswith(".meta.json"):
                continue
            try:
                body = json.loads(path.read_bytes())
            except ValueError:
                continue
            if isinstance(body, dict) and isinstance(body.get("data"), list):
                pages += 1
                keys[tuple(sorted(body))] += 1
                cursors[type_name(body.get("next_cursor"))] += 1
    return keys, cursors, pages


def report_envelope(root: Path, out: Report) -> None:
    keys, cursors, pages = envelope_keys(root)
    out("\n7. Page envelope (every saved appointment page)")
    out(f"   pages: {pages:,}   top-level keys: " + "; ".join(f"{', '.join(k)} x{n:,}" for k, n in keys.most_common()))
    out("   next_cursor types: " + ", ".join(f"{t} {n:,}" for t, n in cursors.most_common()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default="data/raw")
    args = ap.parse_args(argv)
    root = Path(args.root)
    by_id, used = load_all_rows(root)
    if not by_id:
        print("no saved pulls found", file=sys.stderr)
        return 1
    rows = list(by_id.values())
    clinics = load_clinics(root)
    out = Report()
    out("SCHEMA PROFILE   (all-rows variant, newest pull wins per id)")
    out("=" * 72)
    out("pulls read: " + "; ".join(used))
    out(f"unique appointment ids: {len(rows):,}")
    report_fields(rows, len(rows), out)
    report_key_sets(rows, out)
    report_enums(rows, out)
    report_formats(rows, out)
    report_case_and_unicode(rows, clinics, out)
    report_clinics(rows, clinics, out)
    report_envelope(root, out)
    (root / "schema_report.txt").write_text("\n".join(out.lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
