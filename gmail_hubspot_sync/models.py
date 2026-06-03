from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional


@dataclass
class SenderInfo:
    """Dati estratti dall'header From di un'email."""
    email: str
    domain: str
    raw_from: str
    message_id: str
    name: Optional[str] = None
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    company: Optional[str] = None
    subject: str = ""
    received_at: Optional[datetime] = None


@dataclass
class SyncResult:
    """Risultato dell'elaborazione di una singola email."""
    status: Literal["CREATO", "AGGIORNATO", "IGNORATO"]
    email: str
    hubspot_id: Optional[str]
    message_id: str
    reason: str = ""

    def __str__(self) -> str:
        id_part = f"HubSpot ID: {self.hubspot_id}" if self.hubspot_id else "ID: N/A"
        reason_part = f" — {self.reason}" if self.reason else ""
        return f"[{self.status:<10}] {self.email:<40} | {id_part}{reason_part}"
