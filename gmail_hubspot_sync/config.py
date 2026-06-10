import os
from dotenv import load_dotenv

load_dotenv()

# Gmail OAuth scopes
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Path to OAuth credentials file downloaded from Google Cloud Console
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")

# Path where the OAuth token is cached after first login
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

# HubSpot private app access token
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# How often to poll Gmail (seconds)
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# File that stores the last-seen Gmail history ID (for incremental sync)
STATE_FILE = os.getenv("STATE_FILE", ".sync_state.json")

# Label applied to processed Gmail messages
GMAIL_PROCESSED_LABEL = os.getenv("GMAIL_PROCESSED_LABEL", "HubSpot-Synced")

# Domains that should never be imported (internal/noreply addresses)
IGNORED_DOMAINS = {
    "gmail.com",         # personal Gmail — skip to avoid noise; remove if desired
    "googlemail.com",
    "noreply.github.com",
    "notifications.github.com",
    "bounce.notification",
    "mailer-daemon",
    "no-reply",
    "noreply",
    "donotreply",
    "postmaster",
    "maildaemon",
}

# HubSpot contact source label
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"
