"""The API client against a scripted fake HTTP layer: no network, no real waiting."""

import json

import pytest

from nordvet.api.client import ApiClient, ApiError, HttpResponse, TransportError


def ok(body=None, headers=None):
    return HttpResponse(200, headers or {}, json.dumps(body if body is not None else {"data": [], "next_cursor": None}))


def token_ok(value="tok-1"):
    return HttpResponse(200, {}, json.dumps({"access_token": value, "token_type": "Bearer"}))


def status(code, headers=None, text="{}"):
    return HttpResponse(code, headers or {}, text)


class Script:
    """Answers per (method, path) from a list, in order. Records every call."""

    def __init__(self, **by_path):
        self.answers = {path.replace("__", "/"): list(items) for path, items in by_path.items()}
        self.calls = []

    def __call__(self, method, url, *, params, json_body, headers):
        path = url.removeprefix("http://api")
        self.calls.append({"method": method, "path": path, "params": params, "json": json_body, "headers": dict(headers)})
        item = self.answers[path].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


def make(script, clock=None):
    clock = clock or Clock()
    client = ApiClient(
        base_url="http://api",
        client_id="the-id",
        client_secret="the-secret",
        transport=script,
        sleep=clock.sleep,
        clock=clock,
    )
    return client, clock


def test_authenticates_first_and_sends_the_bearer_token_and_params():
    script = Script(**{"__auth__token": [token_ok("tok-1")], "__v1__clinics": [ok()]})
    client, _ = make(script)
    client.get("/v1/clinics", {"a": "1"})
    auth, call = script.calls
    assert auth["method"] == "POST" and auth["json"] == {"client_id": "the-id", "client_secret": "the-secret"}
    assert call["headers"]["Authorization"] == "Bearer tok-1"
    assert call["params"] == {"a": "1"}


def test_token_is_reused_while_young_and_renewed_after_45_minutes():
    script = Script(
        **{"__auth__token": [token_ok("tok-1"), token_ok("tok-2")], "__v1__clinics": [ok(), ok(), ok()]}
    )
    client, clock = make(script)
    client.get("/v1/clinics")
    clock.now += 44 * 60
    client.get("/v1/clinics")
    assert [c["path"] for c in script.calls].count("/auth/token") == 1
    clock.now += 2 * 60  # now 46 minutes old
    client.get("/v1/clinics")
    last = script.calls[-1]
    assert last["headers"]["Authorization"] == "Bearer tok-2"
    assert client.stats.reauths == 1


def test_a_401_gets_one_new_token_and_a_retry():
    script = Script(
        **{
            "__auth__token": [token_ok("tok-1"), token_ok("tok-2")],
            "__v1__clinics": [status(401, text='{"error":"token expired, request a new token"}'), ok()],
        }
    )
    client, _ = make(script)
    assert client.get("/v1/clinics").status == 200
    assert script.calls[-1]["headers"]["Authorization"] == "Bearer tok-2"


def test_a_second_401_is_an_error():
    script = Script(
        **{"__auth__token": [token_ok(), token_ok()], "__v1__clinics": [status(401), status(401)]}
    )
    client, _ = make(script)
    with pytest.raises(ApiError, match="still 401"):
        client.get("/v1/clinics")


def test_429_waits_retry_after_plus_one_and_is_not_a_failure():
    script = Script(
        **{
            "__auth__token": [token_ok()],
            "__v1__clinics": [status(429, {"retry-after": "7"}), status(429, {"retry-after": "3"}), ok()],
        }
    )
    client, clock = make(script)
    assert client.get("/v1/clinics").status == 200
    assert clock.slept == [8, 4]
    assert client.stats.rate_limit_waits == 2 and client.stats.rate_limit_seconds == 12
    assert client.stats.server_retries == 0


def test_429_without_a_usable_retry_after_waits_a_minute():
    script = Script(**{"__auth__token": [token_ok()], "__v1__clinics": [status(429, {"retry-after": "soon"}), ok()]})
    client, clock = make(script)
    client.get("/v1/clinics")
    assert clock.slept == [61]


def test_429_forever_stops_at_the_cap():
    script = Script(**{"__auth__token": [token_ok()], "__v1__clinics": [status(429, {"retry-after": "1"})] * 21})
    client, _ = make(script)
    with pytest.raises(ApiError, match="rate limited"):
        client.get("/v1/clinics")


def test_5xx_and_network_errors_back_off_2_4_8_and_then_succeed():
    script = Script(
        **{
            "__auth__token": [token_ok()],
            "__v1__clinics": [status(500), TransportError("ReadTimeout"), status(503), ok()],
        }
    )
    client, clock = make(script)
    assert client.get("/v1/clinics").status == 200
    assert clock.slept == [2, 4, 8]
    assert client.stats.server_retries == 3


def test_backoff_is_capped_at_30_and_gives_up_after_6_retries():
    script = Script(**{"__auth__token": [token_ok()], "__v1__clinics": [status(500)] * 7})
    client, clock = make(script)
    with pytest.raises(ApiError, match="gave up after 6 retries"):
        client.get("/v1/clinics")
    assert clock.slept == [2, 4, 8, 16, 30, 30]


def test_other_answers_are_handed_back_not_retried():
    for code in (400, 404, 410):
        script = Script(**{"__auth__token": [token_ok()], "__v1__clinics": [status(code)]})
        client, clock = make(script)
        assert client.get("/v1/clinics").status == code
        assert clock.slept == []


def test_failed_authentication_raises_without_leaking_the_secret():
    script = Script(**{"__auth__token": [status(401, text='{"error":"invalid client credentials"}')]})
    client, _ = make(script)
    with pytest.raises(ApiError) as caught:
        client.get("/v1/clinics")
    assert "the-secret" not in str(caught.value) and "the-id" not in str(caught.value)
    assert "the-secret" not in repr(client)


def test_a_token_answer_without_access_token_is_an_error():
    script = Script(**{"__auth__token": [ok({"nope": 1})]})
    client, _ = make(script)
    with pytest.raises(ApiError, match="no access_token"):
        client.get("/v1/clinics")
