import os
from dotenv import load_dotenv

load_dotenv()

# Gmail OAuth
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# HubSpot
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")

# Sync behaviour
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
PROCESSED_IDS_FILE = os.getenv("PROCESSED_IDS_FILE", ".processed_ids.json")
INITIAL_FETCH_LIMIT = int(os.getenv("INITIAL_FETCH_LIMIT", "50"))

# Domains whose senders are treated as personal (no company inferred)
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "live.it",
    "icloud.com", "me.com", "aol.com", "protonmail.com", "proton.me",
    "mail.com", "zoho.com", "yandex.com", "yandex.ru", "gmx.com",
    "inbox.com", "fastmail.com", "fastmail.fm", "tutanota.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it",
}

# Sender prefixes that indicate automated / no-reply senders
AUTOMATED_PREFIXES = (
    "no-reply", "noreply", "mailer-daemon", "postmaster",
    "bounce", "donotreply", "do-not-reply", "notifications",
    "newsletter", "auto-confirm",
)
