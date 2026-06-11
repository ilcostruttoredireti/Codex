from dataclasses import dataclass
from typing import Optional


@dataclass
class EmailSender:
    email: str
    name: Optional[str]
    domain: str
    message_id: str
    subject: str
    date: str


@dataclass
class ContactResult:
    status: str  # "created" | "updated" | "ignored"
    email: str
    contact_id: Optional[str]
    reason: Optional[str] = None
