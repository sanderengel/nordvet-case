"""Settings and secret handling.

Secrets are read from one env file, the first of:
  1. the path in NORDVET_ENV_FILE, if it is set (it must exist);
  2. .env in the repository root (gitignored), the simple choice for someone who clones the repo.
     Docker Compose reads the same file, so the pipeline and the database agree on the password;
  3. ~/.config/nordvet/.env, outside the repository, so a coding agent that can only see the repo
     folder cannot read it. Used only when there is no .env in the repo.
Values already present in the process environment win over the file.

Rules this module enforces for the rest of the code:
  * secrets never appear in repr() or in printed settings,
  * anything that might contain a secret goes through redact() before it is
    printed or logged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

HOME_ENV_FILE = Path("~/.config/nordvet/.env")
REPO_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_API_BASE_URL = "https://vetone-api.kinddesert-e2c4fbdc.westeurope.azurecontainerapps.io"
_DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(RuntimeError):
    """Raised for configuration problems. Messages never contain secret values."""


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    mssql_host: str
    mssql_port: int
    mssql_database: str
    api_client_id: str = field(default="", repr=False)
    api_client_secret: str = field(default="", repr=False)
    mssql_password: str = field(default="", repr=False)

    def missing(self) -> list[str]:
        """Names of required secret variables that are empty."""
        needed = {
            "VETONE_CLIENT_ID": self.api_client_id,
            "VETONE_CLIENT_SECRET": self.api_client_secret,
            "MSSQL_SA_PASSWORD": self.mssql_password,
        }
        return [name for name, value in needed.items() if not value]

    def secret_values(self) -> list[str]:
        return [v for v in (self.api_client_id, self.api_client_secret, self.mssql_password) if v]


def env_file_path() -> Path:
    """The env file to use: NORDVET_ENV_FILE, else the repo's .env, else the file outside the repo."""
    explicit = os.environ.get("NORDVET_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()
    if REPO_ENV_FILE.is_file():
        return REPO_ENV_FILE
    return HOME_ENV_FILE.expanduser()


def load_settings(env_file: Path | None = None) -> Settings:
    path = env_file or env_file_path()
    if not path.is_file():
        if env_file is None and not os.environ.get("NORDVET_ENV_FILE"):
            raise ConfigError(
                f"No env file found at {REPO_ENV_FILE} or {HOME_ENV_FILE}. "
                "Copy .env.template to .env in the repository and fill it in."
            )
        raise ConfigError(f"Env file not found: {path}. Create it from .env.template.")
    load_dotenv(path, override=False)

    port_raw = os.environ.get("MSSQL_PORT", "1433")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError("MSSQL_PORT must be an integer.") from exc

    database = os.environ.get("MSSQL_DATABASE", "nordvet")
    if not _DB_NAME.match(database):
        raise ConfigError("MSSQL_DATABASE may only contain letters, digits and underscores.")

    return Settings(
        api_base_url=os.environ.get("VETONE_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/"),
        mssql_host=os.environ.get("MSSQL_HOST", "localhost"),
        mssql_port=port,
        mssql_database=database,
        api_client_id=os.environ.get("VETONE_CLIENT_ID", ""),
        api_client_secret=os.environ.get("VETONE_CLIENT_SECRET", ""),
        mssql_password=os.environ.get("MSSQL_SA_PASSWORD", ""),
    )


def redact(text: object, settings: Settings) -> str:
    """Replace every known secret value in text with ***."""
    result = str(text)
    for secret in sorted(settings.secret_values(), key=len, reverse=True):
        result = result.replace(secret, "***")
    return result
