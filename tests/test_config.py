"""Unit tests for configuration and secret handling. No network, no database."""

import os

import pytest

from nordvet import config
from nordvet.config import ConfigError, env_file_path, load_settings, redact

ENV_VARS = (
    "VETONE_API_BASE_URL", "VETONE_CLIENT_ID", "VETONE_CLIENT_SECRET",
    "MSSQL_SA_PASSWORD", "MSSQL_HOST", "MSSQL_PORT", "MSSQL_DATABASE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    saved = dict(os.environ)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
    # load_settings() fills os.environ directly (load_dotenv), which monkeypatch does not undo.
    # Put it back, so values from a test's temporary env file cannot leak into later tests.
    os.environ.clear()
    os.environ.update(saved)


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_missing_env_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.env")


def test_loads_values_and_defaults(tmp_path):
    path = write_env(tmp_path, "VETONE_CLIENT_ID=id1\nVETONE_CLIENT_SECRET=sec1\nMSSQL_SA_PASSWORD=Pw0rdLocalOnly\n")
    settings = load_settings(path)
    assert settings.api_client_id == "id1"
    assert settings.mssql_host == "localhost"
    assert settings.mssql_port == 1433
    assert settings.mssql_database == "nordvet"
    assert settings.missing() == []


def test_missing_lists_names_not_values(tmp_path):
    settings = load_settings(write_env(tmp_path, "VETONE_CLIENT_ID=id1\n"))
    assert settings.missing() == ["VETONE_CLIENT_SECRET", "MSSQL_SA_PASSWORD"]


def test_repr_hides_secrets(tmp_path):
    settings = load_settings(write_env(tmp_path, "VETONE_CLIENT_SECRET=supersecretvalue\nMSSQL_SA_PASSWORD=Pw0rdLocalOnly\n"))
    text = repr(settings)
    assert "supersecretvalue" not in text and "Pw0rdLocalOnly" not in text


def test_redact_replaces_all_secrets(tmp_path):
    settings = load_settings(write_env(tmp_path, "VETONE_CLIENT_ID=id1234\nVETONE_CLIENT_SECRET=supersecretvalue\nMSSQL_SA_PASSWORD=Pw0rdLocalOnly\n"))
    message = "login failed for supersecretvalue using Pw0rdLocalOnly and id1234"
    assert redact(message, settings) == "login failed for *** using *** and ***"


def test_rejects_bad_port_and_database_name(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(write_env(tmp_path, "MSSQL_PORT=abc\n"))
    os.environ.pop("MSSQL_PORT", None)  # not monkeypatch: it would "restore" the bad value afterwards
    with pytest.raises(ConfigError):
        load_settings(write_env(tmp_path, "MSSQL_DATABASE=bad;name\n"))


@pytest.fixture
def env_places(tmp_path, monkeypatch):
    """Point the two default env file locations at a temporary folder, with neither file there."""
    home, repo = tmp_path / "home.env", tmp_path / "repo.env"
    monkeypatch.setattr(config, "HOME_ENV_FILE", home)
    monkeypatch.setattr(config, "REPO_ENV_FILE", repo)
    monkeypatch.delenv("NORDVET_ENV_FILE", raising=False)
    return home, repo


def test_the_repo_env_wins_over_the_file_outside_the_repo(env_places):
    home, repo = env_places
    home.write_text("MSSQL_DATABASE=from_home\n")
    assert env_file_path() == home
    repo.write_text("MSSQL_DATABASE=from_repo\n")
    assert env_file_path() == repo
    assert load_settings().mssql_database == "from_repo"


def test_nordvet_env_file_wins_and_must_exist(env_places, tmp_path, monkeypatch):
    home, repo = env_places
    home.write_text("MSSQL_DATABASE=from_home\n")
    repo.write_text("MSSQL_DATABASE=from_repo\n")
    monkeypatch.setenv("NORDVET_ENV_FILE", str(tmp_path / "missing.env"))
    assert env_file_path() == tmp_path / "missing.env"
    with pytest.raises(ConfigError, match="missing.env"):
        load_settings()


def test_no_env_file_anywhere_names_both_places(env_places):
    home, repo = env_places
    with pytest.raises(ConfigError) as err:
        load_settings()
    assert str(home) in str(err.value) and str(repo) in str(err.value)
