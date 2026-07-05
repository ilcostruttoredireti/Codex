"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails, extracts sender contacts, and syncs to HubSpot.
Avoids duplicates using email as unique key. Skips automated/no-reply senders.
"""

import re

# ─── Patterns for automated/no-reply addresses to skip ───────────────────────
SKIP_PATTERNS = [
    r"^no-?reply@",
    r"^noreply@",
    r"^nobody@",
    r"notify-noreply@",
    r"googlebase-noreply@",
    r"admanager-noreply@",
    r"^do-?not-?reply@",
    r"^mailer-daemon@",
    r"^postmaster@",
    r"^bounce@",
    r"@.*noreply\.",
    r"@.*mailer\.",
]

SKIP_DOMAINS = {
    "youtube.com",
    "google.com",
    "googleapis.com",
    "revolut.com",
    "serpapi.com",
    "academia-mail.com",
    "it-news.adidas.com",
    "moneya.es",
    "email.patreon.com",
    "notification.circle.so",
    "e.feedspot.com",
    "shop.tiktok.com",
}

HUBSPOT_TAG = "Inbound Gmail"
HUBSPOT_SOURCE = "Gmail"


def is_automated_sender(email: str) -> bool:
    """Return True if the sender email looks automated/no-reply."""
    email_lower = email.lower()
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, email_lower):
            return True
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    return domain in SKIP_DOMAINS


def extract_contact(sender_raw: str) -> dict | None:
    """
    Parse a raw From header like 'Name <email@domain.com>' or 'email@domain.com'.
    Returns a dict with email, firstname, lastname, company, or None if automated.
    """
    email = ""
    display_name = ""

    angle_match = re.match(r"^(.+?)\s*<(.+?)>$", sender_raw.strip())
    if angle_match:
        display_name = angle_match.group(1).strip().strip('"')
        email = angle_match.group(2).strip().lower()
    else:
        email = sender_raw.strip().lower()

    if not email or "@" not in email:
        return None

    if is_automated_sender(email):
        return None

    domain = email.split("@")[1]
    local = email.split("@")[0]

    # Derive firstname/lastname from display name or email local part
    firstname = ""
    lastname = ""
    if display_name:
        parts = display_name.split()
        if len(parts) >= 2:
            firstname = parts[0]
            lastname = " ".join(parts[1:])
        else:
            firstname = display_name
    else:
        # Try to extract a name from local part (e.g. "riccardo" → "Riccardo")
        name_candidate = local.replace(".", " ").replace("_", " ").replace("-", " ")
        name_candidate = name_candidate.strip()
        if not any(name_candidate.startswith(p) for p in [
            "info", "support", "hello", "contact", "admin", "team",
            "general", "redazione", "commerciale", "product", "nobody",
            "premium", "monty", "daily", "notify", "servizioclienti",
        ]):
            parts = name_candidate.split()
            firstname = parts[0].capitalize() if parts else ""
            lastname = " ".join(p.capitalize() for p in parts[1:]) if len(parts) > 1 else ""

    # Derive company from domain (strip TLD and common subdomains)
    company = derive_company(domain)

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def derive_company(domain: str) -> str:
    """Extract a human-readable company name from email domain."""
    # Strip common prefixes
    for prefix in ["mail.", "em.", "email.", "news.", "it-news."]:
        if domain.startswith(prefix):
            domain = domain[len(prefix):]

    base = domain.rsplit(".", 1)[0]  # strip TLD
    # Replace hyphens/dots with spaces and title-case
    company = base.replace("-", " ").replace(".", " ").title()
    return company


def build_hubspot_properties(contact: dict) -> dict:
    """Build the HubSpot property payload for a contact."""
    props = {
        "email": contact["email"],
        "leadsource": HUBSPOT_SOURCE,
        "hs_analytics_source_data_1": HUBSPOT_TAG,
    }
    if contact.get("firstname"):
        props["firstname"] = contact["firstname"]
    if contact.get("lastname"):
        props["lastname"] = contact["lastname"]
    if contact.get("company"):
        props["company"] = contact["company"]
    return props


# ─── Run summary structure ────────────────────────────────────────────────────

class SyncResult:
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


def process_threads(threads: list[dict]) -> list[dict]:
    """
    Given a list of Gmail thread objects (from MCP search_threads response),
    extract unique real-human senders to sync into HubSpot.

    Returns a list of contact dicts ready for HubSpot upsert.
    """
    seen_emails: set[str] = set()
    contacts: list[dict] = []

    for thread in threads:
        for message in thread.get("messages", []):
            sender_raw = message.get("sender", "")
            contact = extract_contact(sender_raw)
            if contact and contact["email"] not in seen_emails:
                seen_emails.add(contact["email"])
                contacts.append(contact)

    return contacts


# ─── CLI entry point (for local testing) ─────────────────────────────────────

if __name__ == "__main__":
    print("Gmail → HubSpot Sync — local test mode")
    print("Run this via the Claude Code scheduled routine for full MCP integration.")
    print()

    sample_senders = [
        "Riccardo Belli <riccardo@martes-ai.com>",
        "no-reply@youtube.com",
        "info@mircogasparotto.com",
        "noreply@skool.com",
        "Alessio <Alessio@startupgeeks.it>",
        "no-reply@serpapi.com",
        "redazione@startupitalia.eu",
    ]

    for raw in sample_senders:
        result = extract_contact(raw)
        status = "SKIP (automated)" if result is None else f"→ {result}"
        print(f"  {raw!r:45s}  {status}")
