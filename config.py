"""Configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # HubSpot
    hubspot_access_token: str = field(
        default_factory=lambda: os.environ["HUBSPOT_ACCESS_TOKEN"]
    )

    # Gmail OAuth files
    gmail_credentials_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    )
    gmail_token_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_TOKEN_FILE", "token.json")
    )

    # Polling
    poll_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    )

    # State persistence
    state_file: str = field(
        default_factory=lambda: os.getenv("STATE_FILE", ".sync_state.json")
    )

    # Logging
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO")
    )

    @classmethod
    def from_env(cls) -> "Config":
        """Load config, raising KeyError for missing required variables."""
        return cls()
