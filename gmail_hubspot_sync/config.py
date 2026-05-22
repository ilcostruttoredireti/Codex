"""
Configuration loader — reads from environment variables or .env file.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Gmail OAuth2
    GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    GMAIL_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.modify",
    ]

    # HubSpot
    HUBSPOT_API_KEY: str = os.getenv("HUBSPOT_API_KEY", "")
    HUBSPOT_BASE_URL: str = "https://api.hubapi.com"

    # Sync behaviour
    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    CONTACT_SOURCE_LABEL: str = os.getenv("CONTACT_SOURCE_LABEL", "Gmail")
    CONTACT_TAG: str = os.getenv("CONTACT_TAG", "Inbound Gmail")
    STATE_FILE: str = os.getenv("STATE_FILE", "sync_state.json")

    # Domains to skip (internal / no-reply addresses)
    SKIP_DOMAINS: set[str] = {
        "noreply.com", "no-reply.com", "mailer-daemon.com",
        "bounce.com", "notifications.google.com",
    }
    SKIP_PREFIXES: tuple[str, ...] = (
        "noreply@", "no-reply@", "mailer-daemon@",
        "bounce@", "donotreply@", "postmaster@",
    )
