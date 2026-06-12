from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

_FREE_PROVIDERS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "icloud.com", "live.com", "live.it",
    "msn.com", "aol.com", "me.com", "protonmail.com", "proton.me",
    "tutanota.com", "yandex.com", "yandex.ru", "mail.com", "gmx.com",
    "gmx.it", "zoho.com", "libero.it", "virgilio.it", "tiscali.it",
    "tin.it", "alice.it", "fastwebnet.it", "tim.it", "email.it",
})


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None


def parse_sender(from_header: str) -> Optional[ContactInfo]:
    name, email_addr = parseaddr(from_header)
    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    domain = email_addr.split("@")[1]

    name = name.strip().strip('"').strip("'")
    first_name = last_name = None
    if name:
        parts = name.split(None, 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else None

    return ContactInfo(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=_company_from_domain(domain),
        domain=domain,
    )


def _company_from_domain(domain: str) -> Optional[str]:
    if domain in _FREE_PROVIDERS:
        return None
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2].replace("-", " ").title()
    return None
