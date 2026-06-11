import re
from email.utils import parseaddr
from dataclasses import dataclass
from typing import Optional

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "live.com", "icloud.com", "aol.com",
    "protonmail.com", "proton.me", "mail.com", "gmx.com", "gmx.net",
    "yandex.com", "yandex.ru", "libero.it", "tin.it", "alice.it",
    "virgilio.it", "tiscali.it", "fastwebnet.it", "me.com",
    "googlemail.com", "msn.com", "pm.me",
}

SKIP_PATTERNS = re.compile(
    r"(noreply|no-reply|do-not-reply|donotreply|mailer-daemon|"
    r"bounce|postmaster|admin|support|info|newsletter|notifications?|"
    r"alerts?|updates?|subscriptions?|unsubscribe)",
    re.IGNORECASE,
)


@dataclass
class Contact:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None

    def is_complete(self) -> bool:
        return bool(self.first_name and self.last_name and self.company)


def _clean_name_part(part: str) -> str:
    return re.sub(r"[^\w\s\-\']", "", part).strip()


def extract_contact_from_sender(from_header: str) -> Optional[Contact]:
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()

    if not email or "@" not in email:
        return None

    local, domain = email.split("@", 1)

    if SKIP_PATTERNS.search(local):
        return None

    first_name: Optional[str] = None
    last_name: Optional[str] = None

    if display_name:
        name = display_name.strip('"').strip("'").strip()
        # Remove anything in parentheses
        name = re.sub(r"\(.*?\)", "", name).strip()
        parts = [_clean_name_part(p) for p in name.split() if p]
        parts = [p for p in parts if p]

        if len(parts) >= 2:
            first_name = parts[0]
            last_name = " ".join(parts[1:])
        elif len(parts) == 1:
            first_name = parts[0]

    company: Optional[str] = None
    if domain not in FREE_EMAIL_DOMAINS:
        root = domain.split(".")[0]
        company = root.replace("-", " ").replace("_", " ").title()

    return Contact(
        email=email,
        first_name=first_name or None,
        last_name=last_name or None,
        company=company,
        domain=domain,
    )
