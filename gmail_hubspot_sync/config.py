"""Configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MAX_EMAILS_PER_POLL: int = int(os.getenv("MAX_EMAILS_PER_POLL", "50"))
CONTACT_SOURCE_LABEL: str = os.getenv("CONTACT_SOURCE_LABEL", "Gmail")
GMAIL_PROCESSED_LABEL: str = os.getenv("GMAIL_PROCESSED_LABEL", "hubspot-synced")

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Domains whose senders should be ignored (internal / no-reply addresses)
IGNORED_DOMAINS: set[str] = {
    "noreply.com",
    "no-reply.com",
    "mailer.com",
    "bounce.com",
    "notifications.google.com",
    "accounts.google.com",
    "mail.gmail.com",
}

IGNORED_LOCAL_PARTS: set[str] = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "notifications",
    "newsletter",
    "unsubscribe",
    "support",
    "info",
    "admin",
    "hello",
    "contact",
    "team",
}
