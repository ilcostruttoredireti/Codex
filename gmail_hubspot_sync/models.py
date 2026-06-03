from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SenderContact:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None
    message_id: Optional[str] = None
    subject: Optional[str] = None


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str] = None
    detail: Optional[str] = None

    def __str__(self) -> str:
        parts = [f"[{self.status.value}] {self.email}"]
        if self.hubspot_id:
            parts.append(f"ID HubSpot: {self.hubspot_id}")
        if self.detail:
            parts.append(f"({self.detail})")
        return " | ".join(parts)
