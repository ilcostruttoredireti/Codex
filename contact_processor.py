"""Extract structured contact data from a raw Gmail message dict."""

import re

# Domains that should not be used as company names
_IGNORED_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
        "hotmail.com", "hotmail.it", "outlook.com", "outlook.it", "live.com",
        "live.it", "icloud.com", "me.com", "protonmail.com", "proton.me",
        "aol.com", "libero.it", "virgilio.it", "tiscali.it", "alice.it",
        "fastwebnet.it", "tin.it",
    }
)

_DOMAIN_RE = re.compile(r"@([\w.-]+\.[a-z]{2,})", re.IGNORECASE)


def _extract_domain(email: str) -> str:
    m = _DOMAIN_RE.search(email)
    return m.group(1).lower() if m else ""


def _domain_to_company(domain: str) -> str:
    """Convert domain → human-readable company name heuristic."""
    if not domain or domain in _IGNORED_DOMAINS:
        return ""
    # Strip common subdomains (mail., smtp., etc.)
    parts = domain.split(".")
    if len(parts) > 2 and parts[0] in {"mail", "smtp", "email", "mx", "m"}:
        parts = parts[1:]
    # Take the SLD (second-level domain) and capitalise
    return parts[0].capitalize() if parts else ""


def _split_name(display_name: str) -> tuple[str, str]:
    """
    Split a display name into (first, last).
    Handles "Last, First" (comma-separated) and "First [Middle] Last".
    """
    name = display_name.strip().strip('"').strip("'")
    if not name:
        return "", ""
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return parts[1], parts[0]
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def extract_contact(message: dict) -> "ContactData | None":  # noqa: F821
    """
    Build a ContactData from a Gmail message dict.

    Returns None if the sender email is empty or looks like an automated
    noreply address.
    """
    from hubspot_client import ContactData  # local import to avoid circular deps

    from_email: str = message.get("from_email", "").strip().lower()
    from_name: str = message.get("from_name", "").strip()

    if not from_email:
        return None

    # Skip noreply / automated senders
    local_part = from_email.split("@")[0]
    if any(
        kw in local_part
        for kw in ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster", "bounce")
    ):
        return None

    first_name, last_name = _split_name(from_name)
    domain = _extract_domain(from_email)
    company = _domain_to_company(domain)

    return ContactData(
        email=from_email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        source="Gmail",
        tags=["Inbound Gmail"],
    )
