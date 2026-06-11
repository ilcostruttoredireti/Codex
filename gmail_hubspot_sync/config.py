"""
Configuration for Gmail → HubSpot contact sync.
All secrets loaded from environment variables (never hardcoded).
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Gmail OAuth
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# HubSpot
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# Sync behaviour
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "1"))
CONTACT_SOURCE_LABEL = os.getenv("CONTACT_SOURCE_LABEL", "Gmail")
CONTACT_TAG = os.getenv("CONTACT_TAG", "Inbound Gmail")

# Domains to skip (automated senders, delivery notices, own domain)
SKIP_DOMAINS = {
    "googlemail.com",
    "google.com",
    "facebookmail.com",
    "legalmail.it",
    "postacert.istruzione.it",
    "mailer-daemon",
}

# Own email addresses to skip
OWN_EMAILS = {
    addr.strip().lower()
    for addr in os.getenv("OWN_EMAILS", "").split(",")
    if addr.strip()
}
