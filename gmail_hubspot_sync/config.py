import os
from dotenv import load_dotenv

load_dotenv()

# Gmail OAuth2 credentials
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# HubSpot
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# Sync settings
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")

# Contact source label applied to every synced contact
CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

# Domains to skip (internal / noreply)
SKIP_DOMAINS = {
    "noreply.github.com",
    "notifications.github.com",
    "mailer.hubspot.com",
    "bounce.hubspot.com",
}

# Email addresses to skip entirely
SKIP_EMAILS = {
    "noreply",
    "no-reply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "donotreply",
    "do-not-reply",
}
