"""Data models for Gmail-HubSpot sync."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SyncStatus(Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    ERROR = "Errore"


@dataclass
class ContactInfo:
    """Contact information extracted from an email."""

    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None

    def __post_init__(self):
        # Extract domain from email
        if self.email and "@" in self.email:
            self.domain = self.email.split("@")[1].lower()
            # Derive company from domain (exclude common free email providers)
            free_domains = {
                "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                "live.com", "icloud.com", "me.com", "mac.com",
                "libero.it", "virgilio.it", "tiscali.it", "alice.it",
                "protonmail.com", "tutanota.com", "fastmail.com",
                "aol.com", "msn.com", "googlemail.com",
            }
            if self.domain and self.domain not in free_domains:
                if not self.company:
                    # Use domain root as company name (capitalize)
                    company_raw = self.domain.split(".")[0]
                    self.company = company_raw.capitalize()

    def split_name(self):
        """Split full_name into first and last name if not already set."""
        if self.full_name and not (self.first_name or self.last_name):
            parts = self.full_name.strip().split(None, 1)
            self.first_name = parts[0] if parts else None
            self.last_name = parts[1] if len(parts) > 1 else None
        return self


@dataclass
class SyncResult:
    """Result of processing a single email."""

    status: SyncStatus
    email: str
    hubspot_contact_id: Optional[str] = None
    message_id: Optional[str] = None
    subject: Optional[str] = None
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
