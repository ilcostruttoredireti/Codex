import re
from dataclasses import dataclass, field
from typing import Optional

from config import SKIP_SENDERS, FREE_EMAIL_DOMAINS


@dataclass
class SenderInfo:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None


def extract_sender_info(from_header: str) -> Optional[SenderInfo]:
    """Parse a From: header into structured contact data."""
    if not from_header:
        return None

    from_header = from_header.strip()

    # Match "Display Name <email@domain>" or plain email
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>\s*$', from_header)
    if match:
        display_name = match.group(1).strip()
        email_addr = match.group(2).strip().lower()
    else:
        display_name = None
        email_addr = from_header.lower()

    if "@" not in email_addr:
        return None

    local, domain = email_addr.split("@", 1)

    # Skip automated / system senders
    if any(p in local for p in SKIP_SENDERS):
        return None
    if any(p in domain for p in ("mailer", "bounce", "daemon")):
        return None

    # Company: infer from domain unless it's a free provider
    company: Optional[str] = None
    if domain not in FREE_EMAIL_DOMAINS:
        # Strip common TLDs and convert to title-case
        base = domain.rsplit(".", 2)[0] if domain.count(".") > 1 else domain.split(".")[0]
        company = base.replace("-", " ").replace(".", " ").title()

    # Parse display name into first / last
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    if display_name:
        # Remove common institutional prefixes like "PA-CERTA - PARCO …"
        name_part = re.split(r"\s*[-–]\s*", display_name)[0].strip()
        parts = name_part.split()
        if len(parts) >= 2:
            first_name = parts[0]
            last_name = " ".join(parts[1:])
        elif len(parts) == 1 and not parts[0].isupper():
            first_name = parts[0]

    return SenderInfo(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )
