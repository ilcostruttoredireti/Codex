import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
GMAIL_PROCESSED_LABEL = os.getenv("GMAIL_PROCESSED_LABEL", "HubSpot-Synced")

# Domains to ignore (internal / no-reply addresses)
IGNORED_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "noreply.github.com",
    "mailer-daemon.google.com",
}

IGNORED_EMAIL_PREFIXES = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "notifications",
    "support",
    "hello",
    "info",
    "newsletter",
}

# Gmail OAuth scopes
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]
