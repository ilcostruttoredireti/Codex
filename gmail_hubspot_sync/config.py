import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://mail.google.com/",
]


@dataclass
class Config:
    gmail_credentials_file: str
    gmail_token_file: str
    gmail_scopes: list[str]
    gmail_processed_label: str
    hubspot_access_token: str
    poll_interval: int
    enable_activity_note: bool
    log_level: str


def load_config() -> Config:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN è obbligatorio nel file .env")

    return Config(
        gmail_credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        gmail_token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
        gmail_scopes=GMAIL_SCOPES,
        gmail_processed_label=os.getenv("GMAIL_PROCESSED_LABEL", "HubSpot-Processed"),
        hubspot_access_token=token,
        poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        enable_activity_note=os.getenv("ENABLE_ACTIVITY_NOTE", "true").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
