"""Data models for the Gmail → HubSpot sync pipeline."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"


@dataclass
class SenderContact:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    # raw header value, e.g. "Mario Rossi <mario@acme.com>"
    raw_from: str = ""

    @property
    def full_name(self) -> str:
        parts = [p for p in [self.first_name, self.last_name] if p]
        return " ".join(parts)

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str] = None
    message_id: str = ""
    subject: str = ""
    error: Optional[str] = None

    def __str__(self) -> str:
        id_part = f"  HubSpot ID: {self.hubspot_id}" if self.hubspot_id else ""
        err_part = f"  Errore: {self.error}" if self.error else ""
        return (
            f"[{self.status.value}] {self.email}"
            f"{id_part}{err_part}"
        )
