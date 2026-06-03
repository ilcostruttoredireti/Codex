import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "config/credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "config/token.json")
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_DB_PATH: str = os.getenv("STATE_DB_PATH", "data/sync_state.db")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
ADD_GMAIL_LABEL: bool = os.getenv("ADD_GMAIL_LABEL", "true").lower() == "true"
GMAIL_PROCESSED_LABEL: str = os.getenv("GMAIL_PROCESSED_LABEL", "HubSpot-Synced")
CREATE_EMAIL_ENGAGEMENT: bool = os.getenv("CREATE_EMAIL_ENGAGEMENT", "true").lower() == "true"
INITIAL_LOOKBACK_HOURS: int = int(os.getenv("INITIAL_LOOKBACK_HOURS", "24"))

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

# Domini email gratuiti: non estrarre azienda dal dominio
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "proton.me", "tutanota.com", "zoho.com", "mail.com", "yandex.com",
    "gmx.com", "inbox.com", "fastmail.com", "hey.com", "msn.com",
    "yahoo.it", "libero.it", "virgilio.it", "tiscali.it", "alice.it",
    "tin.it", "inwind.it", "email.it", "googlemail.com",
}

# Indirizzi automatici da ignorare (pattern sul local-part)
SKIP_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
    "mailer-daemon", "bounce", "bounces", "unsubscribe", "newsletter",
    "postmaster", "autoresponder", "auto-reply", "autoreply",
}
