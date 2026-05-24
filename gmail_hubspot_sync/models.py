"""
models.py – lightweight dataclasses shared across the project
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SenderInfo:
    """Parsed sender information extracted from an email header."""

    email: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    company: str = ""
    domain: str = ""

    def __post_init__(self) -> None:
        self.email = self.email.lower().strip()
        if "@" in self.email:
            self.domain = self.email.split("@", 1)[1].lower()

    @property
    def display_name(self) -> str:
        if self.full_name:
            return self.full_name
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts)


@dataclass
class EmailMessage:
    """Minimal representation of a Gmail message."""

    message_id: str
    thread_id: str
    subject: str
    sender: SenderInfo
    received_at: str  # RFC 2822 date string
    snippet: str = ""


@dataclass
class SyncResult:
    """Result of syncing a single contact to HubSpot."""

    status: SyncStatus
    contact_email: str
    hubspot_id: str = ""
    message_id: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "stato": self.status.value,
            "email_contatto": self.contact_email,
            "id_hubspot": self.hubspot_id,
            "id_messaggio": self.message_id,
        }
