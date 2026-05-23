from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", str(BASE_DIR / "credentials.json"))
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", str(BASE_DIR / "token.json"))
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", str(BASE_DIR / "sync_state.json"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
MAX_PROCESSED_IDS_CACHE = int(os.getenv("MAX_PROCESSED_IDS_CACHE", "10000"))

_skip_prefixes_raw = os.getenv(
    "SKIP_EMAIL_PREFIXES",
    "noreply,no-reply,donotreply,mailer-daemon,postmaster,bounce,notifications",
)
SKIP_EMAIL_PREFIXES: list[str] = [p.strip() for p in _skip_prefixes_raw.split(",") if p.strip()]

_skip_domains_raw = os.getenv("SKIP_DOMAINS", "")
SKIP_DOMAINS: set[str] = {d.strip() for d in _skip_domains_raw.split(",") if d.strip()}

# Consumer email providers — domain used as company hint only when NOT in this set
GENERIC_EMAIL_DOMAINS: set[str] = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "live.com", "protonmail.com",
    "libero.it", "virgilio.it", "tin.it", "alice.it",
    "aol.com", "mail.com", "yandex.com", "gmx.com",
}
