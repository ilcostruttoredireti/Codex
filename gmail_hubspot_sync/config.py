"""Configuration loader from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

GMAIL_LABEL_FILTER: str = os.getenv("GMAIL_LABEL_FILTER", "INBOX")

_raw_skip = os.getenv("SKIP_DOMAINS", "noreply.com,no-reply.com,mailer-daemon.google.com")
SKIP_DOMAINS: set[str] = {d.strip().lower() for d in _raw_skip.split(",") if d.strip()}
