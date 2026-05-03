from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SyncStatus(Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    ERROR = "Errore"


@dataclass
class EmailContact:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None
    message_id: Optional[str] = None
    subject: Optional[str] = None

    @property
    def full_name(self) -> Optional[str]:
        parts = [p for p in [self.first_name, self.last_name] if p]
        return " ".join(parts) if parts else None


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_contact_id: Optional[str] = None
    error: Optional[str] = None

    def __str__(self) -> str:
        parts = [
            f"Stato: {self.status.value}",
            f"Email: {self.email}",
        ]
        if self.hubspot_contact_id:
            parts.append(f"ID HubSpot: {self.hubspot_contact_id}")
        if self.error:
            parts.append(f"Errore: {self.error}")
        return " | ".join(parts)
