import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    hubspot_access_token: str
    gmail_credentials_path: str
    gmail_token_path: str
    poll_interval_seconds: int
    state_file: str


def load_config() -> Config:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN environment variable is required")

    return Config(
        hubspot_access_token=token,
        gmail_credentials_path=os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json"),
        gmail_token_path=os.getenv("GMAIL_TOKEN_PATH", "token.json"),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        state_file=os.getenv("STATE_FILE", "sync_state.json"),
    )
