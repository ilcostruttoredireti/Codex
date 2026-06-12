import os
from dotenv import load_dotenv

load_dotenv()

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_PROCESSED_LABEL = os.getenv("GMAIL_PROCESSED_LABEL", "HubSpot Synced")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Domains treated as personal (no company extracted)
PERSONAL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com",
    "icloud.com", "me.com", "mac.com", "aol.com",
    "protonmail.com", "proton.me", "zoho.com", "yandex.com",
    "mail.com", "gmx.com", "inbox.com", "fastmail.com",
    "tutanota.com", "hey.com", "msn.com",
    "libero.it", "alice.it", "virgilio.it", "tiscali.it", "email.it",
})

# Local-parts that identify automated / no-reply senders
AUTOMATED_PREFIXES: frozenset[str] = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "mailer",
    "daemon", "newsletter", "automated", "system",
    "auto-reply", "autoreply",
})
