"""Load configuration from environment variables or a .env file."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no external dependency)."""
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example to .env and fill in your credentials."
        )
    return value


@dataclass
class Config:
    # Gmail OAuth2
    gmail_credentials_path: str
    gmail_token_path: str

    # HubSpot Private App token
    hubspot_access_token: str

    # Optional: HubSpot App ID for timeline events
    hubspot_app_id: str = ""


def load_config(env_file: str = ".env") -> Config:
    _load_dotenv(env_file)
    return Config(
        gmail_credentials_path=os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json"),
        gmail_token_path=os.getenv("GMAIL_TOKEN_PATH", "token.json"),
        hubspot_access_token=_require("HUBSPOT_ACCESS_TOKEN"),
        hubspot_app_id=os.getenv("HUBSPOT_APP_ID", ""),
    )
