import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Gmail OAuth
    GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

    # HubSpot
    HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")

    # Sync settings
    POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # Contact source tag
    CONTACT_SOURCE = "Gmail"
    CONTACT_TAG = "Inbound Gmail"

    # Domains to skip (transactional senders, no-reply, etc.)
    SKIP_DOMAINS = {
        "noreply.com", "no-reply.com", "mailer.com",
        "bounce.com", "notifications.com",
    }
    SKIP_PREFIXES = {"noreply", "no-reply", "mailer-daemon", "postmaster", "bounce"}
