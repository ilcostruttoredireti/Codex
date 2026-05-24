"""
email_parser.py — Funzioni pure di parsing degli header email.

Modulo senza dipendenze esterne (solo stdlib).
Testabile in isolamento senza credenziali Google.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
_NAME_RE = re.compile(r'^"?([^"<]+?)"?\s*<', re.UNICODE)


@dataclass
class SenderInfo:
    """Dati estratti dall'header From di un'email."""

    email: str
    first_name: str
    last_name: str
    full_name: str
    company_domain: str
    message_id: str
    subject: str
    date: str


def parse_from_header(
    from_header: str,
    message_id: str = "",
    subject: str = "",
    date: str = "",
    ignore_domains: frozenset[str] | None = None,
) -> Optional[SenderInfo]:
    """
    Estrae email, nome e dominio dall'header From/Reply-To.

    Returns None se:
    - nessuna email valida trovata
    - il dominio è nella lista ignore_domains
    """
    emails_found = _EMAIL_RE.findall(from_header)
    if not emails_found:
        return None

    sender_email = emails_found[0].lower()
    domain = sender_email.split("@")[-1].lower()

    if ignore_domains and domain in ignore_domains:
        return None

    # Estrai nome dal formato  "Nome Cognome" <email>  o  Nome Cognome <email>
    full_name = ""
    name_match = _NAME_RE.search(from_header)
    if name_match:
        full_name = name_match.group(1).strip().strip('"')

    parts = full_name.split() if full_name else []
    first_name = parts[0] if parts else ""
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    return SenderInfo(
        email=sender_email,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        company_domain=domain,
        message_id=message_id,
        subject=subject,
        date=date,
    )


def company_from_domain(domain: str) -> str:
    """
    Ricava un nome azienda "leggibile" dal dominio.
    Es. 'acme.com' → 'Acme'   'mail.google.com' → 'Google'
    """
    parts = domain.rstrip(".").split(".")
    if len(parts) >= 2:
        candidate = parts[-2]
    else:
        candidate = parts[0]
    return candidate.capitalize()
