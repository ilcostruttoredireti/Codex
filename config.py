import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")

POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE: str = os.getenv("STATE_FILE", "sync_state.json")

# Domains treated as personal (company name not extracted from them)
PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "msn.com", "aol.com",
    "protonmail.com", "proton.me", "mail.com", "yandex.com",
    "gmx.com", "zoho.com",
})

# Sender addresses that should never be synced
IGNORED_SENDER_PATTERNS: tuple[str, ...] = (
    "noreply",
    "no-reply",
    "mailer-daemon",
    "bounce",
    "notifications",
    "donotreply",
    "do-not-reply",
)
