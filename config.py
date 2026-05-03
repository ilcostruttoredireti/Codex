import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE: str = os.getenv("STATE_FILE", "sync_state.json")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

IGNORE_DOMAINS: set = {
    d.strip()
    for d in os.getenv("IGNORE_DOMAINS", "").split(",")
    if d.strip()
}

IGNORE_EMAILS: set = {
    e.strip()
    for e in os.getenv("IGNORE_EMAILS", "").split(",")
    if e.strip()
}

# Domains that are free/personal email providers — domain alone cannot be used as company name.
FREE_EMAIL_DOMAINS: frozenset = frozenset({
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr", "yahoo.de",
    "hotmail.com", "hotmail.it", "hotmail.co.uk", "hotmail.fr",
    "outlook.com", "outlook.it",
    "live.com", "live.it",
    "msn.com",
    "icloud.com", "me.com", "mac.com",
    "libero.it", "virgilio.it", "tiscali.it", "fastwebnet.it",
    "tin.it", "alice.it", "inwind.it", "email.it",
    "aol.com",
    "protonmail.com", "pm.me",
    "tutanota.com", "tutamail.com",
    "mail.com", "zoho.com",
    "ymail.com",
})
