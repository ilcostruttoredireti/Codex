from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SenderInfo:
    email: str
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    domain: str = ""
    company: str = ""


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str] = None
    detail: str = ""
