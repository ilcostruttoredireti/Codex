import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_TOKEN: str = os.environ.get("HUBSPOT_TOKEN", "")
GMAIL_CREDENTIALS_FILE: str = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL_SECONDS: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE: str = os.environ.get("STATE_FILE", "sync_state.json")

# Domains considered personal (no company inferred from them)
PERSONAL_EMAIL_DOMAINS: set[str] = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "live.com", "icloud.com", "me.com",
    "aol.com", "protonmail.com", "proton.me", "libero.it", "tiscali.it",
    "alice.it", "virgilio.it", "tin.it", "fastwebnet.it",
}

# Sender patterns to ignore (system/automated emails)
IGNORED_SENDER_PATTERNS: list[str] = [
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications@",
    "newsletter@", "info@mailchimp", "reply@",
]
