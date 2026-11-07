"""A small fake of the VetOne API, for the loader tests. It has the behaviours the
loader depends on, as found by testing the real API:

  * appointments sorted by (scheduled_at, id); `from`/`to` inclusive local dates;
    `updated_since` inclusive; soft-deleted rows only with include_deleted=true;
  * `limit` clamped to a maximum (500 by default; tests lower it to get several pages);
  * cursors that remember the last row served, so a row written later with an earlier sort key
    is missed by a sequence already past it (as with the real API, which sorts by scheduled_at);
  * cursors that can be made to expire (410);
  * a Date header taken from `self.now`, which tests move forward.

It has the same get(path, params) -> HttpResponse shape as the real client.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from email.utils import format_datetime

from nordvet.api.client import HttpResponse


def appt(id, scheduled, updated, status="booked", clinic="CL-001", deleted=None, **extra):
    """An appointment row as the API sends it. scheduled is local time with offset."""
    row = {
        "id": id,
        "clinic_id": clinic,
        "vet_id": "V0001",
        "patient_ref": "P0000000A",
        "service": "consultation",
        "scheduled_at": scheduled,
        "duration_minutes": 20,
        "status": status,
        "created_at": "2026-10-01T08:00:00Z",
        "updated_at": updated,
    }
    if deleted:
        row["deleted_at"] = deleted
    row.update(extra)
    return row


class FakeApi:
    def __init__(self, appointments, clinics=None, now=None, max_page=500):
        self.appointments = list(appointments)
        self.clinics = clinics if clinics is not None else [
            {
                "id": "CL-001", "name": "Test", "city": "Århus", "country": "DK",
                "timezone": "Europe/Copenhagen", "active": True, "opened_on": "2020-01-01",
            }
        ]
        self.now = now or datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
        self.max_page = max_page
        self.expire_cursors = 0  # the next N uses of a cursor answer 410
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        headers = {"date": format_datetime(self.now, usegmt=True)}
        if path == "/v1/clinics":
            return HttpResponse(200, headers, json.dumps({"data": self.clinics}, ensure_ascii=False))
        assert path == "/v1/appointments", path

        cursor = params.pop("cursor", None)
        if cursor and self.expire_cursors > 0:
            self.expire_cursors -= 1
            return HttpResponse(410, headers, '{"error":"cursor expired, restart pagination without cursor"}')

        rows = [r for r in self.appointments if self._matches(r, params)]
        rows.sort(key=lambda r: (r["scheduled_at"], r["id"]))
        if cursor:
            after = json.loads(base64.b64decode(cursor))["after"]
            rows = [r for r in rows if [r["scheduled_at"], r["id"]] > after]
        limit = min(int(params.get("limit", 100)), self.max_page)
        chunk = rows[:limit]
        more = len(rows) > limit
        last = [chunk[-1]["scheduled_at"], chunk[-1]["id"]] if chunk else None
        next_cursor = base64.b64encode(json.dumps({"after": last}).encode()).decode() if more else None
        body = json.dumps({"data": chunk, "next_cursor": next_cursor}, ensure_ascii=False)
        return HttpResponse(200, headers, body)

    @staticmethod
    def _matches(row, params):
        day = row["scheduled_at"][:10]
        if "from" in params and day < params["from"]:
            return False
        if "to" in params and day > params["to"]:
            return False
        if "updated_since" in params and row["updated_at"] < params["updated_since"].replace("Z", "") + "Z":
            return False
        if row.get("deleted_at") and params.get("include_deleted") != "true":
            return False
        return True
