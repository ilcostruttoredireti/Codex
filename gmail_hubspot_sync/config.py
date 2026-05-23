"""Configuration management via environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Gmail OAuth
    GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    GMAIL_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    ]

    # HubSpot
    HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

    # Sync settings
    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    STATE_FILE: str = os.getenv("STATE_FILE", ".sync_state.json")

    # Contact defaults
    CONTACT_SOURCE: str = "Gmail"
    CONTACT_TAG: str = "Inbound Gmail"

    @classmethod
    def validate(cls) -> None:
        if not cls.HUBSPOT_ACCESS_TOKEN:
            raise ValueError("HUBSPOT_ACCESS_TOKEN is required")
        if not os.path.exists(cls.GMAIL_CREDENTIALS_FILE):
            raise FileNotFoundError(
                f"Gmail credentials file not found: {cls.GMAIL_CREDENTIALS_FILE}\n"
                "Download it from Google Cloud Console → APIs & Services → Credentials."
            )
