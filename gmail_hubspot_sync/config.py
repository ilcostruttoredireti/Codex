"""Configuration management for Gmail-HubSpot sync."""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class GmailConfig:
    """Gmail API configuration."""

    # Path to the OAuth2 credentials file downloaded from Google Cloud Console
    credentials_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    )
    # Token file (auto-generated after first OAuth flow)
    token_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_TOKEN_FILE", "token.json")
    )
    # Gmail scopes required
    scopes: list = field(
        default_factory=lambda: ["https://www.googleapis.com/auth/gmail.readonly"]
    )
    # Poll interval in seconds (default: 60 seconds)
    poll_interval: int = field(
        default_factory=lambda: int(os.getenv("GMAIL_POLL_INTERVAL", "60"))
    )
    # Label to filter (default: INBOX). Use "all" for all mail.
    label: str = field(
        default_factory=lambda: os.getenv("GMAIL_LABEL", "INBOX")
    )
    # Max emails to fetch per poll cycle
    max_results: int = field(
        default_factory=lambda: int(os.getenv("GMAIL_MAX_RESULTS", "50"))
    )


@dataclass
class HubSpotConfig:
    """HubSpot API configuration."""

    # HubSpot Private App Token (recommended over API key)
    access_token: str = field(
        default_factory=lambda: os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    )
    # Contact source label
    contact_source: str = "Gmail"
    # Tag to apply to created/updated contacts
    contact_tag: str = "Inbound Gmail"
    # API base URL
    base_url: str = "https://api.hubapi.com"


@dataclass
class AppConfig:
    """Application-level configuration."""

    gmail: GmailConfig = field(default_factory=GmailConfig)
    hubspot: HubSpotConfig = field(default_factory=HubSpotConfig)

    # State file to track processed message IDs across restarts
    state_file: str = field(
        default_factory=lambda: os.getenv("STATE_FILE", "processed_messages.json")
    )
    # Log level
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO")
    )
    # Whether to run once and exit (False = continuous loop)
    run_once: bool = field(
        default_factory=lambda: os.getenv("RUN_ONCE", "false").lower() == "true"
    )

    def validate(self):
        """Validate required configuration."""
        errors = []
        if not self.hubspot.access_token:
            errors.append("HUBSPOT_ACCESS_TOKEN è obbligatorio")
        if not os.path.exists(self.gmail.credentials_file) and \
           not os.path.exists(self.gmail.token_file):
            errors.append(
                f"File credenziali Gmail non trovato: {self.gmail.credentials_file}"
            )
        if errors:
            raise ValueError(
                "Configurazione incompleta:\n" + "\n".join(f"  - {e}" for e in errors)
            )
        return self
