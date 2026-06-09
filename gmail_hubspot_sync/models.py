from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None

    @property
    def full_name(self) -> str:
        parts = filter(None, [self.first_name, self.last_name])
        return " ".join(parts)

    def to_hubspot_properties(self, source: str = "Gmail") -> dict:
        props: dict = {"email": self.email, "hs_lead_status": "NEW"}
        if self.first_name:
            props["firstname"] = self.first_name
        if self.last_name:
            props["lastname"] = self.last_name
        if self.company:
            props["company"] = self.company
        props["hs_analytics_source"] = source
        return props


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str]
    message_id: str
    reason: Optional[str] = None

    def __str__(self) -> str:
        id_part = f"ID={self.hubspot_id}" if self.hubspot_id else "ID=N/A"
        reason_part = f" ({self.reason})" if self.reason else ""
        return f"[{self.status.value}] {self.email} | {id_part}{reason_part}"
