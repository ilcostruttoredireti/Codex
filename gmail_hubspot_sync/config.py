import os
from dotenv import load_dotenv

load_dotenv()

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

HUBSPOT_ACCESS_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]

GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox -from:me newer_than:1d")
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
STATE_FILE = os.getenv("STATE_FILE", "./processed_messages.json")

# Domains to skip (no-reply, mailer-daemon, own account, etc.)
_raw = os.getenv("SKIP_DOMAINS", "")
SKIP_DOMAINS: set[str] = {
    "mailer-daemon.googlemail.com",
    "googlemail.com",
    *(_raw.split(",") if _raw else []),
}

# Senders to skip entirely
SKIP_SENDERS: set[str] = {
    "mailer-daemon@googlemail.com",
    "noreply@google.com",
    "no-reply@google.com",
}
