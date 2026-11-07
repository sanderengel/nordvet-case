"""VetOne API client (Phase 3.2, step 2a).

What it does, and why (all found by testing, see api-notes.md):
  * gets a token with POST /auth/token and sends it as a Bearer header;
  * gets a NEW token before the old one is 45 minutes old (tokens live 50-60 minutes) and
    once more on a 401;
  * 429: waits Retry-After seconds (plus one) and tries again. This is not counted as a
    failure, because the limit is a time window and waiting always works (capped, so a
    broken limiter cannot hold a run forever);
  * 5xx and network errors: waits 2, 4, 8 ... seconds (at most 30) and tries again, at most
    6 times (D-031), then raises ApiError;
  * every other answer (200, 400, 404, 410 ...) is handed back to the caller, who knows what
    it means (a 410 is a dead cursor, which only the pager can handle).

The HTTP call and the sleep are parameters, so tests run without a network and without
waiting. Secrets never appear in messages or logs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import requests

log = logging.getLogger("nordvet.api")

TIMEOUT_S = 60  # the brief warns of a cold start of about 20 s
TOKEN_MAX_AGE_S = 45 * 60  # tokens were accepted at 50 minutes and rejected at 60
MAX_RETRIES = 6  # retries after a 5xx or network error (D-031)
MAX_RATE_LIMIT_WAITS = 20  # 429 waits per call
DEFAULT_RETRY_AFTER_S = 60
MAX_BACKOFF_S = 30


class ApiError(RuntimeError):
    """The API could not be used. The message never contains a secret."""


class TransportError(RuntimeError):
    """The request did not produce an HTTP response (timeout, connection error)."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]  # keys in lower case
    text: str


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, *, params: dict | None, json_body: dict | None, headers: dict
    ) -> HttpResponse: ...


def requests_transport() -> Transport:
    """The real HTTP layer. Raises TransportError when there is no HTTP answer."""
    session = requests.Session()

    def send(method, url, *, params, json_body, headers):
        try:
            resp = session.request(method, url, params=params, json=json_body, headers=headers, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            raise TransportError(type(exc).__name__) from exc
        # resp.text would guess the encoding; the API sends UTF-8 JSON.
        text = resp.content.decode("utf-8")
        return HttpResponse(resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, text)

    return send


@dataclass
class ClientStats:
    requests: int = 0
    rate_limit_waits: int = 0
    rate_limit_seconds: int = 0
    server_retries: int = 0
    reauths: int = 0


@dataclass
class ApiClient:
    base_url: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    transport: Transport = field(default_factory=requests_transport, repr=False)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    stats: ClientStats = field(default_factory=ClientStats)
    _token: str | None = field(default=None, init=False, repr=False)
    _token_at: float = field(default=0.0, init=False, repr=False)

    # ------------------------------------------------------------------ public

    def get(self, path: str, params: dict | None = None) -> HttpResponse:
        """GET with the token. Returns whatever the API answered after the retries described above."""
        self._ensure_token()
        reauthed = False
        while True:
            resp = self._send_with_retries("GET", path, params=params, token=self._token)
            if resp.status != 401:
                return resp
            if reauthed:
                raise ApiError(f"GET {path}: still 401 after authenticating again")
            log.warning("401 on GET %s: authenticating again", path)
            reauthed = True
            self._authenticate()

    # ------------------------------------------------------------------ token

    def _ensure_token(self) -> None:
        if self._token is None or self.clock() - self._token_at >= TOKEN_MAX_AGE_S:
            self._authenticate()

    def _authenticate(self) -> None:
        if self._token is not None:
            self.stats.reauths += 1
        resp = self._send_with_retries(
            "POST",
            "/auth/token",
            json_body={"client_id": self.client_id, "client_secret": self.client_secret},
        )
        if resp.status != 200:
            raise ApiError(f"authentication failed: HTTP {resp.status}")
        try:
            token = json.loads(resp.text)["access_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError("authentication failed: no access_token in the answer") from exc
        self._token = token
        self._token_at = self.clock()

    # ------------------------------------------------------------------ one call with retries

    def _send_with_retries(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        token: str | None = None,
    ) -> HttpResponse:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = self.base_url + path

        failures = 0
        rate_limit_waits = 0
        while True:
            self.stats.requests += 1
            try:
                resp = self.transport(method, url, params=params, json_body=json_body, headers=headers)
            except TransportError as exc:
                resp = None
                problem = f"network error ({exc})"
            else:
                if resp.status == 429:
                    rate_limit_waits += 1
                    if rate_limit_waits > MAX_RATE_LIMIT_WAITS:
                        raise ApiError(f"{method} {path}: still rate limited after {MAX_RATE_LIMIT_WAITS} waits")
                    wait = _retry_after(resp) + 1
                    log.info("429 on %s %s: waiting %ds (Retry-After)", method, path, wait)
                    self.stats.rate_limit_waits += 1
                    self.stats.rate_limit_seconds += wait
                    self.sleep(wait)
                    continue
                if resp.status < 500:
                    return resp
                problem = f"HTTP {resp.status}"

            failures += 1
            if failures > MAX_RETRIES:
                raise ApiError(f"{method} {path}: {problem}, gave up after {MAX_RETRIES} retries")
            wait = min(2**failures, MAX_BACKOFF_S)
            log.warning("%s on %s %s: waiting %ds, retry %d/%d", problem, method, path, wait, failures, MAX_RETRIES)
            self.stats.server_retries += 1
            self.sleep(wait)


def _retry_after(resp: HttpResponse) -> int:
    try:
        return max(0, int(resp.headers.get("retry-after", DEFAULT_RETRY_AFTER_S)))
    except ValueError:
        return DEFAULT_RETRY_AFTER_S
