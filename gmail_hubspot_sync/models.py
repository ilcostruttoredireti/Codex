from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


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
    source_email_subject: Optional[str] = None
    source_email_date: Optional[str] = None

    def __post_init__(self):
        if self.email:
            self.email = self.email.lower().strip()
        if not self.domain and self.email and "@" in self.email:
            self.domain = self.email.split("@")[1]


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_contact_id: Optional[str] = None
    error: Optional[str] = None
    details: str = ""
