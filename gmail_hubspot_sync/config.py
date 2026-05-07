"""Configuration loaded from environment variables."""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

COMMON_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "msn.com", "aol.com",
    "protonmail.com", "mail.com", "gmx.com", "yandex.com",
})


@dataclass
class Config:
    # Gmail OAuth
    gmail_credentials_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    )
    gmail_token_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_TOKEN_FILE", "token.json")
    )
    gmail_scopes: list[str] = field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
        ]
    )

    # HubSpot
    hubspot_api_key: str = field(
        default_factory=lambda: os.getenv("HUBSPOT_API_KEY", "")
    )
    hubspot_contact_source: str = "Gmail"
    hubspot_tag: str = "Inbound Gmail"

    # Sync behaviour
    poll_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    )
    state_file: str = field(
        default_factory=lambda: os.getenv("STATE_FILE", "sync_state.json")
    )

    def validate(self) -> None:
        if not self.hubspot_api_key:
            raise ValueError("HUBSPOT_API_KEY environment variable is required")
        if not os.path.exists(self.gmail_credentials_file):
            raise FileNotFoundError(
                f"Gmail credentials file not found: {self.gmail_credentials_file}\n"
                "Download it from Google Cloud Console and save as credentials.json"
            )
