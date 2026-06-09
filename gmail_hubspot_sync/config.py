import os
from dataclasses import dataclass, field
from typing import List


GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

PROCESSED_LABEL = "HubSpot-Synced"

# Domains whose senders should be ignored (auto-mailers, no-reply, etc.)
IGNORED_SENDER_PREFIXES = (
    "no-reply",
    "noreply",
    "do-not-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "notifications",
    "newsletter",
    "info+",
    "system",
)

IGNORED_DOMAINS = {
    "mailer-daemon.googlemail.com",
    "bounces.google.com",
}


@dataclass
class Config:
    gmail_credentials_file: str
    gmail_token_file: str
    hubspot_access_token: str
    poll_interval_seconds: int
    max_emails_per_cycle: int
    contact_source: str

    @classmethod
    def from_env(cls) -> "Config":
        hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
        if not hubspot_token:
            hubspot_token = os.getenv("HUBSPOT_API_KEY", "")

        return cls(
            gmail_credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
            gmail_token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
            hubspot_access_token=hubspot_token,
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            max_emails_per_cycle=int(os.getenv("MAX_EMAILS_PER_CYCLE", "50")),
            contact_source=os.getenv("CONTACT_SOURCE", "Gmail"),
        )

    def validate(self) -> None:
        if not self.hubspot_access_token:
            raise ValueError(
                "HUBSPOT_ACCESS_TOKEN (or HUBSPOT_API_KEY) env variable is required"
            )
        if not os.path.exists(self.gmail_credentials_file):
            raise FileNotFoundError(
                f"Gmail credentials file not found: {self.gmail_credentials_file}\n"
                "Download OAuth 2.0 credentials from Google Cloud Console and save as credentials.json"
            )
