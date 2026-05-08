"""Configuration loaded from environment variables."""
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
    hubspot_api_key: str = field(
        default_factory=lambda: os.environ.get("HUBSPOT_API_KEY", "")
    )

    # Polling
    poll_interval_seconds: int = field(
        default_factory=lambda: int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
    )

    # Sender domains to ignore (comma-separated, e.g. gmail.com,yahoo.com)
    ignored_domains: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        raw = os.environ.get("IGNORED_DOMAINS", "")
        if raw:
            self.ignored_domains = [d.strip().lower() for d in raw.split(",") if d.strip()]

    def validate(self) -> None:
        if not self.hubspot_api_key:
            raise ValueError("HUBSPOT_API_KEY environment variable is required")
        if not os.path.exists(self.gmail_credentials_file):
            raise FileNotFoundError(
                f"Gmail credentials file not found: {self.gmail_credentials_file}. "
                "Download it from Google Cloud Console."
            )
