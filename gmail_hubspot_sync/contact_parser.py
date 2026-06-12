import re
from dataclasses import dataclass
from typing import Optional, Tuple

# Provider email gratuiti / personali: non usare il dominio come nome azienda
_GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr", "yahoo.de", "yahoo.es",
    "hotmail.com", "hotmail.it", "hotmail.co.uk", "hotmail.fr",
    "outlook.com", "outlook.it",
    "live.com", "live.it",
    "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me",
    "mail.com", "aol.com", "msn.com", "ymail.com",
    "libero.it", "alice.it", "tin.it", "virgilio.it",
    "tiscali.it", "fastwebnet.it", "inwind.it",
}


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    full_name: Optional[str]
    company: Optional[str]
    domain: str


def _parse_from_header(from_header: str) -> Tuple[Optional[str], str]:
    """Restituisce (nome_visualizzato, indirizzo_email) dall'header From:."""
    from_header = from_header.strip()

    # Formato: "Nome Cognome" <email@domain.com>  o  Nome Cognome <email@domain.com>
    match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>', from_header)
    if match:
        name = match.group(1).strip() or None
        email = match.group(2).strip().lower()
        return name, email

    # Formato: email pura
    return None, from_header.lower()


def _split_name(full_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Divide un nome completo in (nome, cognome)."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], None
    return None, None


def domain_to_company(domain: str) -> Optional[str]:
    """
    Converte il dominio email in nome azienda.
    Restituisce None per provider generici (gmail, yahoo, ecc.).
    """
    domain = domain.lower()
    if domain in _GENERIC_DOMAINS:
        return None

    parts = domain.split(".")
    # Per TLD composti (es. co.uk) prendiamo la parte -2, altrimenti -2 rispetto al TLD
    company_part = parts[-2] if len(parts) >= 2 else parts[0]
    # Pulizia: trattini e underscore → spazi, title-case
    company_name = re.sub(r"[-_]", " ", company_part).strip().title()
    return company_name or None


def extract_contact_from_email(from_header: str) -> ContactInfo:
    """Estrae le informazioni del mittente dall'header From: di Gmail."""
    full_name, email = _parse_from_header(from_header)

    domain = email.split("@")[-1] if "@" in email else ""
    company = domain_to_company(domain) if domain else None

    first_name, last_name = None, None
    if full_name:
        first_name, last_name = _split_name(full_name)

    return ContactInfo(
        email=email,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        company=company,
        domain=domain,
    )
