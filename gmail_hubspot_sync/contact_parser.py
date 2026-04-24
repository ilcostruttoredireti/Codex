"""
Estrae e normalizza i dati del mittente da un'intestazione email.
"""

import re
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.utils import parseaddr
from typing import Optional


# Domini email personali: non vengono usati come nome azienda
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "hotmail.com",
    "hotmail.it", "outlook.com", "outlook.it", "live.com", "live.it",
    "icloud.com", "me.com", "mac.com", "aol.com", "libero.it",
    "virgilio.it", "alice.it", "tin.it", "fastwebnet.it", "tiscali.it",
    "protonmail.com", "pm.me",
}


@dataclass
class SenderContact:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None
    raw_from: str = ""

    @property
    def full_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "company": self.company,
            "domain": self.domain,
        }


def parse_sender(from_header: str) -> Optional[SenderContact]:
    """Ricava email, nome e azienda dall'header From di un'email."""
    if not from_header:
        return None

    display_name, email_address = parseaddr(from_header)

    # Pulizia base
    email_address = email_address.strip().lower()
    display_name = display_name.strip().strip('"').strip("'")

    if not _is_valid_email(email_address):
        return None

    domain = _extract_domain(email_address)
    first_name, last_name = _split_display_name(display_name)
    company = _infer_company(domain, display_name)

    return SenderContact(
        email=email_address,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
        raw_from=from_header,
    )


def _is_valid_email(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))


def _extract_domain(email: str) -> Optional[str]:
    parts = email.split("@")
    return parts[1] if len(parts) == 2 else None


def _split_display_name(name: str) -> tuple[Optional[str], Optional[str]]:
    """Suddivide il nome visualizzato in nome e cognome."""
    if not name:
        return None, None

    # Rimuovi parentesi e contenuto tra parentesi es. "Mario Rossi (CEO)"
    name = re.sub(r"\(.*?\)", "", name).strip()

    # Formato "Cognome, Nome"
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return parts[1] or None, parts[0] or None

    tokens = name.split()
    if len(tokens) == 1:
        return tokens[0], None
    if len(tokens) >= 2:
        return tokens[0], " ".join(tokens[1:])

    return None, None


def _infer_company(domain: Optional[str], display_name: str) -> Optional[str]:
    """Ricava il nome dell'azienda dal dominio, se non personale."""
    if not domain or domain in _PERSONAL_DOMAINS:
        return None

    # Rimuovi TLD e sottodomini comuni
    parts = domain.split(".")
    # Considera solo il secondo livello: es. "acme.co.uk" → "acme"
    if len(parts) >= 2:
        company_part = parts[-2] if len(parts) == 2 else parts[-3] if parts[-2] in ("co", "com", "org", "net") else parts[-2]
        return company_part.capitalize()

    return None
