import email.utils
from dataclasses import dataclass

# Provider email personali da escludere come nome azienda
_PERSONAL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr", "yahoo.es",
    "hotmail.com", "hotmail.it", "hotmail.fr", "hotmail.co.uk",
    "outlook.com", "outlook.it",
    "live.com", "live.it", "msn.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "proton.me",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it",
    "fastwebnet.it", "tin.it", "email.it", "inwind.it",
})


@dataclass
class ContactData:
    email: str
    first_name: str | None
    last_name: str | None
    company: str | None
    domain: str


def parse_from_header(from_header: str) -> ContactData | None:
    """Estrae dati strutturati dall'header 'From' di un'email."""
    if not from_header:
        return None

    display_name, email_addr = email.utils.parseaddr(from_header)

    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    domain = email_addr.split("@")[1]

    first_name, last_name = _split_display_name(display_name.strip())
    company = _company_from_domain(domain)

    return ContactData(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )


def _split_display_name(name: str) -> tuple[str | None, str | None]:
    """Divide il nome visualizzato in nome e cognome."""
    name = name.strip('"\'')
    if not name:
        return None, None

    parts = name.split(None, 1)
    first = _capitalize(parts[0]) if parts else None
    last = _capitalize(parts[1]) if len(parts) > 1 else None
    return first, last


def _capitalize(s: str) -> str | None:
    s = s.strip()
    if not s:
        return None
    # Preserva maiuscole se già in CamelCase/ALL-CAPS, altrimenti titolifica
    if s.isupper() or s.islower():
        return s.title()
    return s


def _company_from_domain(domain: str) -> str | None:
    """Ricava il nome azienda dal dominio email (solo per domini aziendali)."""
    if domain in _PERSONAL_DOMAINS:
        return None

    # Prende solo la parte prima del primo punto: 'acme.co.uk' → 'acme'
    slug = domain.split(".")[0]
    if not slug:
        return None

    return slug.replace("-", " ").replace("_", " ").title()
