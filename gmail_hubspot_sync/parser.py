import re
from typing import Optional
from .models import ContactInfo

# Patterns that indicate automated/system senders to skip
SKIP_PATTERNS = re.compile(
    r"^(no[-_]?reply|noreply|notifications?|nobody|donotreply|bounce|mailer[-_]?daemon|"
    r"postmaster|devnull|blackhole|spam|junk|abuse|unsubscribe)@",
    re.IGNORECASE,
)

# Functional prefixes that are not personal names
ROLE_PREFIXES = {
    "info", "formazione", "commerciale", "support", "staff", "product",
    "ciao", "general", "confirm", "hello", "contact", "team", "admin",
    "help", "sales", "marketing", "billing", "newsletter", "news",
    "press", "media", "pr", "hr", "legal", "finance", "accounts",
}

# Map of domain fragments to company names
COMPANY_OVERRIDES = {
    "yoast.com": "Yoast",
    "ifttt.com": "IFTTT",
    "canva.com": "Canva",
    "recharge.com": "Recharge",
    "mailchimp.com": "Mailchimp",
    "serpapi.com": "SerpApi",
    "openai.com": "OpenAI",
    "vercel.com": "Vercel",
    "discord.com": "Discord",
    "youtube.com": "YouTube",
    "linkedin.com": "LinkedIn",
    "boolean.careers": "Boolean Careers",
    "mediacloud.press": "Media Cloud Press",
    "scuolaecommerce.com": "Scuola Ecommerce",
    "spiegamelofacile.com": "Spiegamelofacile",
    "bdmassociati.it": "BDM Associati",
    "raffaprivatejet.com": "Raffa Private Jet",
    "boolean.careers": "Boolean Careers",
    "feedspot.com": "Feedspot",
}


def should_skip(email: str) -> bool:
    return bool(SKIP_PATTERNS.match(email))


def _domain_to_company(domain: str) -> str:
    if domain in COMPANY_OVERRIDES:
        return COMPANY_OVERRIDES[domain]
    # Strip subdomain layers: engage.canva.com → canva.com
    parts = domain.split(".")
    for i in range(len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in COMPANY_OVERRIDES:
            return COMPANY_OVERRIDES[candidate]
    # Fallback: capitalise base domain name
    base = parts[-2] if len(parts) >= 2 else parts[0]
    return base.replace("-", " ").title()


def _parse_name(local: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (first_name, last_name) from the local part of an email address."""
    local_lower = local.lower()
    if local_lower in ROLE_PREFIXES:
        return None, None

    # chelsea.c → Chelsea, C
    if "." in local:
        parts = [p.capitalize() for p in local.split(".") if p]
        if len(parts) == 1:
            return parts[0], None
        return parts[0], " ".join(parts[1:])

    # firstname_lastname or firstname-lastname
    for sep in ("_", "-"):
        if sep in local:
            parts = [p.capitalize() for p in local.split(sep) if p]
            if len(parts) >= 2:
                return parts[0], " ".join(parts[1:])

    return local.capitalize(), None


def parse_sender(raw_sender: str, subject: str = "", date: str = "") -> Optional[ContactInfo]:
    """
    Parse a raw Gmail 'From' header into a ContactInfo.
    Returns None if the sender should be skipped.
    """
    # Handle "Display Name <email>" format
    match = re.match(r'^(?:"?([^"<>]+)"?\s+)?<([^>]+)>$', raw_sender.strip())
    if match:
        display_name = (match.group(1) or "").strip()
        email = match.group(2).strip().lower()
    elif "@" in raw_sender:
        display_name = ""
        email = raw_sender.strip().lower()
    else:
        return None

    if should_skip(email):
        return None

    local, domain = email.split("@", 1)
    company = _domain_to_company(domain)

    # Prefer display name for name parsing
    first_name: Optional[str] = None
    last_name: Optional[str] = None

    if display_name:
        name_parts = display_name.split()
        if name_parts:
            first_name = name_parts[0].capitalize()
            if len(name_parts) > 1:
                last_name = " ".join(name_parts[1:]).capitalize()
    else:
        first_name, last_name = _parse_name(local)

    return ContactInfo(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
        source_email_subject=subject,
        source_email_date=date,
    )
