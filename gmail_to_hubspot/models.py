from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SenderInfo:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None

    @classmethod
    def from_raw(cls, raw_from: str) -> "SenderInfo":
        """Parse 'Nome Cognome <email@domain.com>' or plain 'email@domain.com'."""
        raw_from = raw_from.strip()
        name_part = ""
        email_part = ""

        if "<" in raw_from and ">" in raw_from:
            name_part = raw_from[: raw_from.index("<")].strip().strip('"')
            email_part = raw_from[raw_from.index("<") + 1 : raw_from.index(">")].strip()
        else:
            email_part = raw_from

        email_part = email_part.lower()
        domain = email_part.split("@")[1] if "@" in email_part else None

        first_name = last_name = None
        if name_part:
            parts = name_part.split(None, 1)
            first_name = parts[0] if parts else None
            last_name = parts[1] if len(parts) > 1 else None

        company = _domain_to_company(domain) if domain else None

        return cls(
            email=email_part,
            first_name=first_name,
            last_name=last_name,
            company=company,
            domain=domain,
        )


def _domain_to_company(domain: str) -> Optional[str]:
    """Best-effort company name from domain (strip TLD, capitalise)."""
    _skip = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
              "live.com", "me.com", "googlemail.com", "protonmail.com", "libero.it",
              "virgilio.it", "tiscali.it", "alice.it", "tin.it"}
    if not domain or domain.lower() in _skip:
        return None
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


@dataclass
class SyncResult:
    status: SyncStatus
    contact_email: str
    hubspot_id: Optional[str] = None
    message: str = ""
