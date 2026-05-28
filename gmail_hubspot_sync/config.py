import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    # Gmail OAuth credentials (from Google Cloud Console)
    gmail_credentials_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    )
    gmail_token_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_TOKEN_FILE", "token.json")
    )

    # HubSpot private app token
    hubspot_token: str = field(
        default_factory=lambda: os.getenv("HUBSPOT_TOKEN", "")
    )

    # How often to poll Gmail (seconds)
    poll_interval: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL", "60"))
    )

    # File to persist the last processed history ID
    state_file: str = field(
        default_factory=lambda: os.getenv("STATE_FILE", ".sync_state.json")
    )

    # Domains to skip (no-reply bots, internal, etc.)
    skip_domains: tuple = (
        "googlemail.com",
        "mailer-daemon.googlemail.com",
        "noreply.github.com",
        "notifications.github.com",
    )

    # Emails to skip entirely (self, bots)
    skip_addresses: tuple = ()

    def __post_init__(self):
        own_email = os.getenv("OWN_EMAIL", "")
        if own_email:
            self.skip_addresses = (own_email,)

    @classmethod
    def from_env(cls) -> "Config":
        return cls()
