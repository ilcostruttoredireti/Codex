import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

_NO_REPLY_RE = re.compile(
    r"no.?reply|noreply|do.not.reply|donotreply|mailer.daemon|postmaster|bounce|"
    r"notifications?|alert|automated|daemon|system|support\+|help\+",
    re.IGNORECASE,
)

_PERSONAL_DOMAINS = frozenset(
    {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "icloud.com", "aol.com", "protonmail.com", "live.com",
        "me.com", "mac.com", "ymail.com", "msn.com", "googlemail.com",
    }
)

_KNOWN_COMPANIES = {
    "github.com": "GitHub",
    "notion.so": "Notion",
    "slack.com": "Slack",
    "google.com": "Google",
    "microsoft.com": "Microsoft",
    "amazon.com": "Amazon",
    "apple.com": "Apple",
    "meta.com": "Meta",
    "linkedin.com": "LinkedIn",
    "twitter.com": "Twitter",
    "x.com": "X (Twitter)",
    "stripe.com": "Stripe",
    "shopify.com": "Shopify",
    "hubspot.com": "HubSpot",
    "salesforce.com": "Salesforce",
    "zoom.us": "Zoom",
    "atlassian.com": "Atlassian",
}


@dataclass
class SenderContact:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    domain: str
    raw_from: str


def parse_sender(from_header: str) -> Optional[SenderContact]:
    display_name, email_addr = parseaddr(from_header)

    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    local, domain = email_addr.rsplit("@", 1)

    if _NO_REPLY_RE.search(local):
        return None

    first_name, last_name = _extract_name(display_name.strip(), local)
    company = _infer_company(domain)

    return SenderContact(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
        raw_from=from_header,
    )


def _extract_name(
    display_name: str, local_part: str
) -> tuple[Optional[str], Optional[str]]:
    name = display_name.strip('"\'').strip()

    if name and not _looks_like_email(name):
        parts = name.split()
        if len(parts) >= 2:
            return _capitalize(parts[0]), _capitalize(" ".join(parts[1:]))
        if len(parts) == 1:
            return _capitalize(parts[0]), None

    # Fallback: parse local part (john.doe → John, Doe)
    clean = re.sub(r"[^a-z.]", "", local_part)
    if "." in clean:
        parts = clean.split(".")
        return _capitalize(parts[0]), _capitalize(parts[-1])

    return None, None


def _looks_like_email(s: str) -> bool:
    return "@" in s


def _infer_company(domain: str) -> Optional[str]:
    if domain in _PERSONAL_DOMAINS:
        return None
    if domain in _KNOWN_COMPANIES:
        return _KNOWN_COMPANIES[domain]
    # Strip TLD and capitalise the registrable part
    parts = domain.split(".")
    return _capitalize(parts[-2]) if len(parts) >= 2 else _capitalize(domain)


def _capitalize(s: str) -> str:
    return s.capitalize() if s else s
