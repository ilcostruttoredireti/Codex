"""Modelli dati condivisi tra i moduli."""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ProcessedEmail:
    """Rappresenta un'email già processata (per evitare duplicati in sessione)."""
    message_id: str
    sender_email: str
    contact_id: str
    status: str
