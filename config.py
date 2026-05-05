import os
from dotenv import load_dotenv

load_dotenv()

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "aol.com", "protonmail.com",
    "mail.com", "gmx.com", "yandex.com", "libero.it",
    "virgilio.it", "tiscali.it", "alice.it", "msn.com",
    "yahoo.it", "googlemail.com",
}

SKIP_LOCAL_PARTS = {
    "noreply", "no-reply", "mailer-daemon", "postmaster",
    "bounce", "notifications", "donotreply", "do-not-reply",
    "newsletter", "info", "support", "help",
}
