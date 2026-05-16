"""Configuration management via environment variables."""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # Gmail OAuth2
    gmail_credentials_file: str = field(
        default_factory=lambda: os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    )
    gmail_token_file: str = field(
        default_factory=lambda: os.environ.get("GMAIL_TOKEN_FILE", "token.json")
    )

    # HubSpot
    hubspot_token: str = field(
        default_factory=lambda: os.environ.get("HUBSPOT_TOKEN", "")
    )

    # Sync settings
    poll_interval_seconds: int = field(
        default_factory=lambda: int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
    )
    state_file: str = field(
        default_factory=lambda: os.environ.get("STATE_FILE", "sync_state.json")
    )

    # Labels / tags to ignore (e.g. your own sent mail)
    ignored_domains: list = field(
        default_factory=lambda: [
            d.strip()
            for d in os.environ.get("IGNORED_DOMAINS", "").split(",")
            if d.strip()
        ]
    )

    contact_source: str = "Gmail"
    contact_tag: str = "Inbound Gmail"

    def validate(self) -> None:
        if not self.hubspot_token:
            raise ValueError("HUBSPOT_TOKEN environment variable is required.")
        if not os.path.exists(self.gmail_credentials_file):
            raise FileNotFoundError(
                f"Gmail credentials file not found: {self.gmail_credentials_file}\n"
                "Download it from Google Cloud Console → APIs & Services → Credentials."
            )
