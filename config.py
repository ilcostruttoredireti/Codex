"""
Central configuration — reads from environment variables or .env file.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── HubSpot ──────────────────────────────────────────────────────────────────
HUBSPOT_TOKEN: str = os.getenv("HUBSPOT_TOKEN", "")

# ── Gmail ─────────────────────────────────────────────────────────────────────
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.readonly"]

# ── Sync behaviour ────────────────────────────────────────────────────────────
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "60"))          # seconds
INITIAL_FETCH_LIMIT: int = int(os.getenv("INITIAL_FETCH_LIMIT", "50"))
STATE_FILE: str = os.getenv("STATE_FILE", "sync_state.json")

# ── Filtering ─────────────────────────────────────────────────────────────────
# Free / personal email domains — no company can be inferred from them
PERSONAL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "live.com", "msn.com", "aol.com",
    "protonmail.com", "proton.me", "fastmail.com", "mail.com",
    "yandex.com", "zoho.com", "gmx.com", "gmx.net",
    "yahoo.it", "yahoo.fr", "yahoo.co.uk", "yahoo.de",
    "libero.it", "alice.it", "tin.it", "virgilio.it",
})

# Local-part patterns that indicate automated / no-reply senders
IGNORED_LOCAL_PATTERNS: frozenset[str] = frozenset({
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notifications", "notification", "mailer", "bounce",
    "postmaster", "newsletter", "updates", "alerts",
    "daemon", "mailerdaemon", "mailer-daemon",
})

# ── HubSpot constants ─────────────────────────────────────────────────────────
HUBSPOT_BASE_URL: str = "https://api.hubapi.com"
# Standard HubSpot association type: Note → Contact
NOTE_TO_CONTACT_ASSOC_TYPE_ID: int = 202
