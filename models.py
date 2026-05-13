from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SenderInfo:
    email: str
    domain: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None


@dataclass
class SyncResult:
    status: SyncStatus
    email: Optional[str] = None
    contact_id: Optional[str] = None
    reason: Optional[str] = None

    def __str__(self) -> str:
        parts = [f"[{self.status.value}]"]
        if self.email:
            parts.append(f"email={self.email}")
        if self.contact_id:
            parts.append(f"id={self.contact_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " | ".join(parts)
