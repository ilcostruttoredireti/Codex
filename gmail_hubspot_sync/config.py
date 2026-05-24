"""
config.py – centralised configuration loaded from environment / .env file
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env if present (safe to call even if the file is missing)
load_dotenv()


class Config:
    # ── HubSpot ──────────────────────────────────────────────
    HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

    # ── Gmail OAuth2 ─────────────────────────────────────────
    GMAIL_CREDENTIALS_PATH: str = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
    GMAIL_TOKEN_PATH: str = os.getenv("GMAIL_TOKEN_PATH", "token.json")
    GMAIL_MONITORED_ADDRESS: str = os.getenv("GMAIL_MONITORED_ADDRESS", "")

    # Scopes needed: read mail + manage labels
    GMAIL_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.modify",
    ]

    # ── Sync behaviour ───────────────────────────────────────
    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    GMAIL_PROCESSED_LABEL: str = os.getenv("GMAIL_PROCESSED_LABEL", "HubSpot-Synced")
    SYNC_SINCE_DATE: str = os.getenv("SYNC_SINCE_DATE", "")

    # HubSpot contact source / tag constants
    CONTACT_SOURCE: str = "Gmail"
    CONTACT_TAG: str = "Inbound Gmail"

    # Domains treated as personal (no company extraction)
    PERSONAL_DOMAINS: frozenset[str] = frozenset(
        {
            "gmail.com",
            "yahoo.com",
            "hotmail.com",
            "outlook.com",
            "live.com",
            "icloud.com",
            "me.com",
            "aol.com",
            "protonmail.com",
            "proton.me",
            "libero.it",
            "tiscali.it",
            "virgilio.it",
            "alice.it",
            "tin.it",
        }
    )

    # ── Logging ──────────────────────────────────────────────
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_FILE: str = os.getenv("LOG_FILE", "sync.log")

    def validate(self) -> None:
        """Raise ValueError if required variables are missing."""
        errors: list[str] = []
        if not self.HUBSPOT_ACCESS_TOKEN:
            errors.append("HUBSPOT_ACCESS_TOKEN is not set")
        if not Path(self.GMAIL_CREDENTIALS_PATH).exists():
            errors.append(
                f"GMAIL_CREDENTIALS_PATH '{self.GMAIL_CREDENTIALS_PATH}' not found"
            )
        if errors:
            raise ValueError("Configuration errors:\n  - " + "\n  - ".join(errors))


config = Config()
