"""
Configuration for Gmail → HubSpot contact sync.
All secrets are loaded from environment variables or a .env file.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Gmail OAuth2 ──────────────────────────────────────────────────────────────
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",  # needed to add labels
]

# ── HubSpot ───────────────────────────────────────────────────────────────────
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# ── Sync behaviour ────────────────────────────────────────────────────────────
# How many seconds to wait between polling cycles
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Gmail label applied to every processed message (created automatically if missing)
PROCESSED_LABEL = os.getenv("PROCESSED_LABEL", "HubSpot-Synced")

# Source label stored on HubSpot contacts
HUBSPOT_CONTACT_SOURCE = "Gmail"

# Tag appended to the HubSpot contact notes
HUBSPOT_TAG = "Inbound Gmail"

# Domains to ignore (your own domain, no-reply addresses, etc.)
IGNORED_DOMAINS = {
    d.strip()
    for d in os.getenv("IGNORED_DOMAINS", "noreply.com,mailer-daemon.com").split(",")
    if d.strip()
}

# Email prefixes to ignore
IGNORED_PREFIXES = {
    p.strip()
    for p in os.getenv(
        "IGNORED_PREFIXES", "noreply,no-reply,donotreply,do-not-reply,mailer-daemon"
    ).split(",")
    if p.strip()
}

# State file: keeps track of the last Gmail history ID to avoid re-processing
STATE_FILE = os.getenv("STATE_FILE", ".sync_state.json")
