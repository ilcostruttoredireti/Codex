"""Shared data models."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SenderInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    domain: str
    company: Optional[str]
    message_id: str
    subject: str
