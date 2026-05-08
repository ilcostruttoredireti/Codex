"""Pure-Python helpers shared between gmail_client and hubspot_client."""

import re

PUBLIC_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "icloud.com", "live.com", "msn.com", "protonmail.com",
    "aol.com", "zoho.com", "mail.com",
}

NO_REPLY_RE = re.compile(r"(no.?reply|noreply|donotreply|mailer-daemon)", re.I)
_SENDER_RE = re.compile(r'^"?([^"<]*?)"?\s*<([^>]+)>$')


def parse_sender(from_header: str) -> dict:
    """Return {'name', 'email', 'domain'} from a raw From header value."""
    from_header = from_header.strip()
    match = _SENDER_RE.match(from_header)
    if match:
        name = match.group(1).strip()
        addr = match.group(2).strip().lower()
    else:
        name = ""
        addr = from_header.lower()

    domain = addr.split("@")[1] if "@" in addr else ""
    return {"name": name, "email": addr, "domain": domain}


def extract_name_parts(name: str) -> tuple[str, str]:
    """Split display name into (first_name, last_name)."""
    parts = name.strip().split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return name.strip(), ""


def company_from_domain(domain: str) -> str:
    """Guess a company name from an email domain (empty for public providers)."""
    if not domain or domain.lower() in PUBLIC_DOMAINS:
        return ""
    return domain.split(".")[0].capitalize()


def is_no_reply(email: str) -> bool:
    return bool(NO_REPLY_RE.search(email))
