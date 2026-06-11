"""Parsing dei mittenti email per estrarre dati contatto strutturati."""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

# Domini email personali — non derivare il nome azienda da questi
_PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "zoho.com", "mail.com", "libero.it",
    "alice.it", "tin.it", "virgilio.it", "tiscali.it", "fastweb.it",
    "gmx.com", "gmx.de", "web.de",
}

# Indirizzi automatici/sistema da ignorare (confronto sul localpart)
SYSTEM_SENDERS = {
    "noreply", "no-reply", "do-not-reply", "donotreply",
    "notifications", "newsletter", "mailer-daemon", "postmaster",
    "bounce", "bounces", "unsubscribe", "abuse",
    "support", "help", "info", "admin", "administrator",
    "sales", "marketing", "hello", "contact", "team",
    "billing", "invoice", "invoicing", "accounts",
    "news", "updates", "alerts", "auto", "automated",
}


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    domain: str

    def is_complete(self) -> bool:
        return bool(self.first_name and self.last_name and self.company)


def parse_sender(from_header: str) -> Optional[ContactInfo]:
    """Analizza l'header From e restituisce un ContactInfo, o None se non parsabile."""
    display_name, email_addr = parseaddr(from_header)
    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.strip().lower()
    if not _is_valid_email(email_addr):
        return None

    domain = email_addr.split("@")[1]
    first_name, last_name = _split_display_name(display_name.strip())
    company = _company_from_domain(domain)

    return ContactInfo(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )


def is_system_sender(email: str) -> bool:
    """Restituisce True se l'email sembra provenire da un sistema automatico."""
    local = email.split("@")[0].lower()
    local_clean = re.sub(r"[^a-z]", "", local)
    return any(s in local_clean for s in SYSTEM_SENDERS) or local_clean in SYSTEM_SENDERS


def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def _split_display_name(name: str) -> tuple[Optional[str], Optional[str]]:
    if not name:
        return None, None
    # Rimuovi eventuali virgolette
    name = name.strip('"\'')
    parts = name.split(None, 1)
    if len(parts) == 0:
        return None, None
    if len(parts) == 1:
        return parts[0].capitalize(), None
    return parts[0].capitalize(), parts[1].title()


def _company_from_domain(domain: str) -> Optional[str]:
    """Ricava il nome azienda dal dominio email, ignorando i domini personali."""
    if domain in _PERSONAL_DOMAINS:
        return None
    # Rimuove il TLD e i sottodomini, usa il secondo livello come nome
    parts = domain.split(".")
    base = parts[-2] if len(parts) >= 2 else parts[0]
    return base.replace("-", " ").replace("_", " ").title()
