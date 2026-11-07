"""The load command's exit codes and the database retry, with the database and API replaced."""

import logging

import pytest

from nordvet import config
from nordvet.db.connection import ensure_database_with_retry
from nordvet.ingest import load


def settings(**over):
    base = dict(
        api_base_url="http://api", mssql_host="h", mssql_port=1, mssql_database="d",
        api_client_id="the-id", api_client_secret="the-secret", mssql_password="the-pass",
    )
    base.update(over)
    return config.Settings(**base)


class Conn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def wired(monkeypatch):
    """main() with everything outside it replaced; returns the connection it will use."""
    conn = Conn()
    monkeypatch.setattr(load, "load_settings", lambda: settings())
    monkeypatch.setattr(load, "ensure_database_with_retry", lambda s: False)
    monkeypatch.setattr(load, "connect", lambda s: conn)
    monkeypatch.setattr(
        load, "run_load",
        lambda c, client, full=False: load.LoadSummary(7, full, None, 3, 12, 0, None, []),
    )
    return conn


def test_success_exits_0_and_closes_the_connection(wired):
    assert load.main([]) == 0 and wired.closed


def test_config_problems_exit_1(monkeypatch, wired):
    def boom():
        raise config.ConfigError("Env file not found")

    monkeypatch.setattr(load, "load_settings", boom)
    assert load.main([]) == 1
    monkeypatch.setattr(load, "load_settings", lambda: settings(api_client_secret=""))
    assert load.main([]) == 1


def test_a_failed_run_exits_2_closes_the_connection_and_hides_secrets(monkeypatch, wired, caplog):
    def fail(conn, client, full=False):
        raise RuntimeError("login failed for the-pass")

    monkeypatch.setattr(load, "run_load", fail)
    with caplog.at_level(logging.INFO):
        assert load.main([]) == 2
    assert wired.closed
    assert "the-pass" not in caplog.text and "***" in caplog.text


def test_an_unreachable_database_exits_2(monkeypatch, wired):
    def down(s):
        raise OSError("no route")

    monkeypatch.setattr(load, "ensure_database_with_retry", down)
    assert load.main([]) == 2


def test_full_flag_reaches_run_load(monkeypatch, wired):
    seen = {}

    def spy(conn, client, full=False):
        seen["full"] = full
        return load.LoadSummary(1, full, None, 1, 0, 0, None, [])

    monkeypatch.setattr(load, "run_load", spy)
    load.main(["--full"])
    assert seen["full"] is True


def test_database_retry_waits_and_then_succeeds():
    calls, slept = [], []

    def flaky(s):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("error during handshakes before login")
        return True

    assert ensure_database_with_retry(settings(), tries=5, wait_s=5, sleep=slept.append, ensure=flaky) is True
    assert slept == [5, 5]


def test_database_retry_gives_up_with_the_last_error():
    def down(s):
        raise RuntimeError("still down")

    slept = []
    with pytest.raises(RuntimeError, match="still down"):
        ensure_database_with_retry(settings(), tries=3, wait_s=1, sleep=slept.append, ensure=down)
    assert len(slept) == 2
