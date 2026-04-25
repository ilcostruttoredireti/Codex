"""
Configuration and environment variable loading for the Gmail → HubSpot sync service.
"""
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # Gmail OAuth2 credentials (from Google Cloud Console)
    gmail_credentials_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    )
    gmail_token_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_TOKEN_FILE", "token.json")
    )
    # Scopes: read-only access to Gmail messages
    gmail_scopes: list = field(
        default_factory=lambda: ["https://www.googleapis.com/auth/gmail.readonly"]
    )

    # HubSpot private app token (Settings → Integrations → Private Apps)
    hubspot_access_token: str = field(
        default_factory=lambda: os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    )

    # Polling interval in seconds (default: 60s)
    poll_interval: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL", "60"))
    )

    # File used to persist the last-processed Gmail history ID
    state_file: str = field(
        default_factory=lambda: os.getenv("STATE_FILE", ".sync_state.json")
    )

    # Tag added to every contact created/updated by this service
    inbound_tag: str = "Inbound Gmail"
    contact_source: str = "Gmail"

    # Domains to ignore (your own company domains, known no-reply senders, etc.)
    ignored_domains: set = field(
        default_factory=lambda: {
            d.strip().lower()
            for d in os.getenv("IGNORED_DOMAINS", "gmail.com,noreply.com,no-reply.com").split(",")
            if d.strip()
        }
    )


def load_config() -> Config:
    return Config()
