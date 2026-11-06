"""Discovery probe for the VetOne API (Phase 2).

Purpose: save a small sample of RAW responses to disk so they can be read by
hand. This is a throwaway-style exploration tool, not the ingestion code.

    python -m nordvet.explore.explore            # the core calls
    python -m nordvet.explore.explore --errors   # plus deliberately bad requests

Output goes to data/raw/<UTC timestamp>/ (gitignored). For every call:
  NN_<step>.json       the response body as received
  NN_<step>.meta.json  status, timing, the request path and parameters, and a
                       short allow-list of response headers
The token endpoint is the exception: only the SHAPE of its response is saved
(key names, types, expiry fields), never the token value.

Secret handling (see D-012):
  * request headers are never written anywhere,
  * the client secret and the access token are scrubbed from everything that is
    printed or saved (meta.json records if a body had to be scrubbed),
  * no retries, so failures show up exactly as the API produces them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from nordvet.config import ConfigError, Settings, load_settings, redact

TIMEOUT_S = 60  # the brief warns about a ~20 s cold start
SAFE_HEADER_PREFIXES = (
    "content-type",
    "content-length",
    "date",
    "etag",
    "cache-control",
    "retry-after",
    "link",
    "www-authenticate",
    "x-ratelimit",
    "ratelimit",
)
SHAPE_MAX_STRING = 40
SHAPE_MAX_LIST_ITEMS = 3


def mask_shape(obj: object, key: str = "") -> object:
    """Describe a JSON value without leaking secrets.

    Long strings and anything stored under a key that mentions token or secret
    are replaced by '<str, N chars>' (token_type is kept). Short scalars (such as token_type or
    expires_in) are kept, since they are what we want to learn.
    """
    if isinstance(obj, dict):
        return {k: mask_shape(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_shape(v, key) for v in obj[:SHAPE_MAX_LIST_ITEMS]]
    if isinstance(obj, str):
        lowered = key.lower()
        secret_like = ("token" in lowered and lowered != "token_type") or "secret" in lowered
        if secret_like or len(obj) > SHAPE_MAX_STRING:
            return f"<str, {len(obj)} chars>"
    return obj


class Probe:
    def __init__(self, settings: Settings, out_dir: Path) -> None:
        self.settings = settings
        self.out_dir = out_dir
        self.session = requests.Session()
        self.token: str | None = None
        self.counter = 0
        self.quiet = False  # no per-call line (used for long paging loops)
        self.reauths = 0  # how often a 401 forced a new token
        self.rate_limit_waits = 0  # how often a 429 made us wait
        self.rate_limit_seconds = 0
        self.server_retries = 0  # retries after a 5xx or a network error

    def scrub(self, text: str) -> str:
        text = redact(text, self.settings)
        if self.token:
            text = text.replace(self.token, "***")
        return text

    def call(
        self,
        step: str,
        method: str,
        path: str,
        *,
        params: dict | str | None = None,
        json_body: dict | None = None,
        token: str | None = None,
        shape_only: bool = False,
    ) -> tuple[requests.Response | None, object]:
        self.counter += 1
        name = f"{self.counter:04d}_{step}"
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        started = time.monotonic()
        try:
            resp = self.session.request(
                method,
                self.settings.api_base_url + path,
                params=params,
                json=json_body,
                headers=headers,
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as exc:
            print(f"{name:<38} FAILED: {self.scrub(type(exc).__name__ + ': ' + str(exc))}")
            return None, None
        elapsed = time.monotonic() - started

        body = resp.content
        parsed: object = None
        try:
            parsed = json.loads(body)
        except ValueError:
            pass

        scrubbed = False
        if shape_only:
            out_bytes = json.dumps(mask_shape(parsed), indent=2).encode()
            body_file = f"{name}.shape.json"
        else:
            try:
                text = body.decode("utf-8")
                clean = self.scrub(text)
                scrubbed = clean != text
                out_bytes = clean.encode("utf-8")
            except UnicodeDecodeError:
                out_bytes = body
            ext = "json" if parsed is not None else ("html" if body.lstrip()[:1] == b"<" else "txt")
            body_file = f"{name}.{ext}"
        (self.out_dir / body_file).write_bytes(out_bytes)

        meta = {
            "step": step,
            "method": method,
            "path": path,
            "params": params or {},
            "status": resp.status_code,
            "elapsed_s": round(elapsed, 2),
            "bytes": len(body),
            "body_saved_as": body_file,
            "body_is_shape_only": shape_only,
            "body_was_scrubbed": scrubbed,
            "response_headers": {
                k: v
                for k, v in resp.headers.items()
                if k.lower().startswith(SAFE_HEADER_PREFIXES)
            },
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (self.out_dir / f"{name}.meta.json").write_text(json.dumps(meta, indent=2))

        if not self.quiet:
            self._summarise(name, resp.status_code, elapsed, len(body), parsed)
        return resp, parsed

    def _summarise(self, name: str, status: int, elapsed: float, size: int, parsed: object) -> None:
        extra = ""
        if isinstance(parsed, dict):
            keys = ",".join(str(k) for k in parsed)
            extra = f"keys=[{keys}]"
            data = parsed.get("data")
            if isinstance(data, list):
                extra += f" rows={len(data)}"
            if "error" in parsed:
                extra += " error=" + self.scrub(str(parsed["error"]))[:80]
        elif isinstance(parsed, list):
            extra = f"list rows={len(parsed)}"
        print(f"{name:<38} {status}  {elapsed:5.1f}s  {size:>7}B  {extra}")

    def authenticate(self) -> bool:
        resp, parsed = self.call(
            "auth_token",
            "POST",
            "/auth/token",
            json_body={
                "client_id": self.settings.api_client_id,
                "client_secret": self.settings.api_client_secret,
            },
            shape_only=True,
        )
        if resp is None or resp.status_code != 200 or not isinstance(parsed, dict):
            return False
        candidates = [k for k, v in parsed.items() if isinstance(v, str) and "token" in k.lower()]
        key = "access_token" if "access_token" in candidates else (candidates or [None])[0]
        if key is None:
            print("No token-like field in the auth response; see the .shape.json file.")
            return False
        self.token = parsed[key]
        return True


def run_core(p: Probe) -> None:
    p.call("clinics", "GET", "/v1/clinics", token=p.token)
    p.call("appointments_default", "GET", "/v1/appointments", token=p.token)
    _, page1 = p.call(
        "appointments_limit10", "GET", "/v1/appointments", params={"limit": 10}, token=p.token
    )
    cursor = page1.get("next_cursor") if isinstance(page1, dict) else None
    if cursor:
        p.call(
            "appointments_limit10_page2",
            "GET",
            "/v1/appointments",
            params={"limit": 10, "cursor": cursor},
            token=p.token,
        )
    else:
        print("(no next_cursor in the limit=10 response; page 2 skipped)")
    p.call(
        "appointments_include_deleted",
        "GET",
        "/v1/appointments",
        params={"limit": 10, "include_deleted": "true"},
        token=p.token,
    )
    p.call(
        "appointments_old_range",
        "GET",
        "/v1/appointments",
        params={"limit": 10, "from": "2025-01-01", "to": "2025-01-07"},
        token=p.token,
    )


def run_errors(p: Probe) -> None:
    print("\n-- deliberately bad requests --")
    p.call("err_clinics_no_auth", "GET", "/v1/clinics")
    p.call("err_clinics_bad_token", "GET", "/v1/clinics", token="not-a-real-token")
    p.call(
        "err_bad_cursor", "GET", "/v1/appointments", params={"cursor": "garbage"}, token=p.token
    )
    p.call(
        "err_bad_date", "GET", "/v1/appointments", params={"from": "not-a-date"}, token=p.token
    )
    p.call("err_negative_limit", "GET", "/v1/appointments", params={"limit": -1}, token=p.token)
    p.call(
        "err_huge_limit", "GET", "/v1/appointments", params={"limit": 100000}, token=p.token
    )
    p.call(
        "err_bad_credentials",
        "POST",
        "/auth/token",
        json_body={"client_id": "nobody", "client_secret": "wrong"},
        shape_only=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nordvet.explore.explore", description=__doc__.split("\n")[0])
    parser.add_argument("--errors", action="store_true", help="also send deliberately bad requests")
    parser.add_argument("--out-dir", default="data/raw", help="parent folder for the sample")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 2
    missing = [m for m in settings.missing() if m.startswith("VETONE_")]
    if missing:
        print("Missing in the env file: " + ", ".join(missing))
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out_dir) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Saving to {out_dir}\n")

    probe = Probe(settings, out_dir)
    if not probe.authenticate():
        print("\nAuthentication failed; nothing else was tried.")
        return 1
    run_core(probe)
    if args.errors:
        run_errors(probe)
    print(f"\nDone. Files are in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
