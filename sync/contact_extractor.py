from email.utils import parseaddr
from .models import SenderInfo

_PERSONAL_DOMAINS = {
    "gmail", "yahoo", "hotmail", "outlook", "icloud",
    "protonmail", "live", "aol", "libero", "tiscali",
    "virgilio", "alice", "fastwebnet", "tin",
}

_NOREPLY_PATTERNS = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notification",
    "automated", "robot",
)


def extract_sender(from_header: str) -> SenderInfo:
    """Parse a From: header into a SenderInfo."""
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()

    parts = display_name.strip().split()
    first_name = parts[0] if parts else ""
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    domain = email.split("@")[1] if "@" in email else ""

    company = ""
    if domain:
        base = domain.split(".")[0]
        if base not in _PERSONAL_DOMAINS:
            company = base.capitalize()

    return SenderInfo(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )


def is_noreply(email: str) -> bool:
    local = email.split("@")[0].lower()
    return any(p in local for p in _NOREPLY_PATTERNS)


def is_valid_email(email: str) -> bool:
    return bool(email) and "@" in email and "." in email.split("@")[-1]
