"""Smoke tests: is everything reachable? Prints status only, never secret values.

Checks the configuration, that the API answers (GET /docs, which needs no login; the first call
can be slow after the API has been idle) and the database (created if missing). A quick check
before the first load; it does not log in to the API.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import requests

from nordvet.config import ConfigError, Settings, load_settings, redact
from nordvet.db.connection import connect, ensure_database

OK, WARN, FAIL = "OK", "WARN", "FAIL"
Result = tuple[str, str]


def check_config(settings: Settings) -> Result:
    missing = settings.missing()
    if missing:
        return FAIL, "empty in env file: " + ", ".join(missing)
    return OK, "env file loaded, all required variables are set (values not shown)"


def check_api(settings: Settings) -> Result:
    url = f"{settings.api_base_url}/docs"
    start = time.perf_counter()
    try:
        # First call can take ~20 s while the API scales up from idle.
        response = requests.get(url, timeout=(10, 60))
    except requests.RequestException as exc:
        return FAIL, f"GET /docs failed: {redact(type(exc).__name__, settings)}"
    elapsed = time.perf_counter() - start
    detail = f"GET /docs -> HTTP {response.status_code} in {elapsed:.1f}s"
    if response.status_code >= 400:
        return FAIL, detail
    if elapsed > 10:
        return WARN, detail + " (cold start; later calls should be faster)"
    return OK, detail


def check_database(settings: Settings) -> Result:
    try:
        created = ensure_database(settings)
        conn = connect(settings)
        try:
            cur = conn.cursor()
            cur.execute("SELECT DATEDIFF(day, '2026-01-01', '2026-01-10')")
            value = cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:  # driver errors vary; report class and redacted message
        return FAIL, f"{type(exc).__name__}: {redact(exc, settings)}"
    if value != 9:
        return FAIL, f"unexpected query result: {value}"
    note = "created database" if created else "database exists"
    return OK, f"connected, T-SQL query returned the expected value ({note}: {settings.mssql_database})"


def run_smoke() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[FAIL] config: {exc}")
        return 1

    checks: list[tuple[str, Callable[[Settings], Result]]] = [
        ("config", check_config),
        ("api", check_api),
        ("database", check_database),
    ]
    failed = False
    for name, check in checks:
        status, message = check(settings)
        print(f"[{status:<4}] {name}: {message}")
        failed = failed or status == FAIL
    return 1 if failed else 0
