import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE: str = os.getenv("STATE_FILE", ".sync_state.json")

# Dominio email generici da escludere per l'inferenza del nome azienda
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "aol.com", "protonmail.com",
    "mail.com", "ymail.com", "msn.com", "googlemail.com",
    "yahoo.it", "libero.it", "virgilio.it", "tiscali.it",
    "alice.it", "tin.it", "fastwebnet.it",
}

# Pattern mittenti da ignorare (no-reply, mailing list, ecc.)
IGNORED_SENDER_PATTERNS = [
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications",
    "newsletter", "info@", "support@", "admin@",
]
