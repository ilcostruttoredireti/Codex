"""Central configuration loaded from environment / .env file."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── HubSpot ──────────────────────────────────────────────────────────────
    HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    HUBSPOT_BASE_URL: str = "https://api.hubapi.com"

    # ── Gmail OAuth ──────────────────────────────────────────────────────────
    GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    GMAIL_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",  # needed to apply labels
    ]

    # ── Sync settings ────────────────────────────────────────────────────────
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    STATE_FILE: str = os.getenv("STATE_FILE", ".sync_state.json")
    GMAIL_SYNCED_LABEL: str = os.getenv("GMAIL_SYNCED_LABEL", "HubSpot-Synced")
    NEW_CONTACT_LIFECYCLE: str = os.getenv("NEW_CONTACT_LIFECYCLE", "lead")

    SKIP_FREE_DOMAINS: set[str] = set(
        d.strip().lower()
        for d in os.getenv(
            "SKIP_FREE_DOMAINS",
            "gmail.com,yahoo.com,hotmail.com,outlook.com,live.com,icloud.com",
        ).split(",")
        if d.strip()
    )
    SKIP_NOREPLY: bool = os.getenv("SKIP_NOREPLY", "true").lower() == "true"

    @classmethod
    def validate(cls) -> None:
        missing = []
        if not cls.HUBSPOT_ACCESS_TOKEN:
            missing.append("HUBSPOT_ACCESS_TOKEN")
        if not Path(cls.GMAIL_CREDENTIALS_FILE).exists():
            missing.append(f"GMAIL_CREDENTIALS_FILE ({cls.GMAIL_CREDENTIALS_FILE} not found)")
        if missing:
            raise EnvironmentError(
                "Missing required configuration:\n  " + "\n  ".join(missing)
                + "\nCopy .env.example to .env and fill in the values."
            )
