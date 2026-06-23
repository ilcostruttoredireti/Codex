"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages, extracts sender contacts, and syncs to HubSpot.
Run on a schedule (e.g. every 15 minutes via cron or Claude Code /loop).
"""

import re
from dataclasses import dataclass, field
from typing import Optional


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ContactInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    source_email_subject: str = ""
    source_email_date: str = ""


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: str
    detail: str = ""


# ── Email parsing helpers ──────────────────────────────────────────────────────

# Emails to skip (user's own accounts)
IGNORED_SENDERS = {
    "cristian.mameli@gmail.com",
    "cristian.mameli.editore@gmail.com",
}

# Domains that indicate personal (non-business) email — no company derivable
PERSONAL_DOMAINS = {"gmail.com", "hotmail.com", "yahoo.com", "virgilio.it",
                    "libero.it", "outlook.com", "icloud.com", "me.com"}


def parse_sender(sender_raw: str) -> tuple[str, str]:
    """
    Parse Gmail 'From' header into (name, email).
    Handles:  '"Name" <email>' , 'Name <email>', 'email'
    """
    m = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', sender_raw.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    return "", sender_raw.strip().lower()


def derive_company(email: str, name: str) -> str:
    """Best-effort company name from email domain."""
    domain = email.split("@")[-1].lower()
    if domain in PERSONAL_DOMAINS:
        return ""
    # Strip TLD(s) and capitalise
    parts = domain.split(".")
    # Drop trailing .it, .com, .org, .net, .eu, etc. (keep subdomain logic)
    # e.g. consiglio.regione.lombardia.it → "Consiglio Regionale Lombardia"
    meaningful = [p for p in parts if p not in
                  {"it", "com", "org", "net", "eu", "io", "co"}]
    return " ".join(p.capitalize() for p in meaningful) if meaningful else ""


def split_fullname(display_name: str) -> tuple[str, str]:
    """Split 'Firstname Lastname' → (firstname, lastname)."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def extract_contact(thread: dict) -> Optional[ContactInfo]:
    """
    Extract contact from a Gmail thread dict (as returned by MCP search_threads).
    Handles forwarded messages by looking at snippet body for embedded 'Da:' headers.
    """
    messages = thread.get("messages", [])
    if not messages:
        return None

    msg = messages[0]
    sender = msg.get("sender", "")
    snippet = msg.get("snippet", "")
    subject = msg.get("subject", "")
    date = msg.get("date", "")

    name, email = parse_sender(sender)

    # Skip user's own forwarding accounts
    if email in IGNORED_SENDERS:
        # Try to extract original sender from snippet ("Da: ..." in Italian)
        original = _extract_forwarded_sender(snippet)
        if original:
            email = original["email"]
            name = original["name"]
        else:
            return None

    # Skip if still one of the user's accounts after extraction
    if email in IGNORED_SENDERS or not email:
        return None

    first, last = split_fullname(name) if name else ("", "")
    company = derive_company(email, name)

    return ContactInfo(
        email=email,
        firstname=first,
        lastname=last,
        company=company,
        source_email_subject=subject,
        source_email_date=date,
    )


def _extract_forwarded_sender(snippet: str) -> Optional[dict]:
    """
    Parse embedded 'Da "Name" email' or 'Da: Name <email>' from a forwarded snippet.
    Returns {"name": str, "email": str} or None.
    """
    # Pattern: Da "Name" email  or  Da: "Name" email
    m = re.search(
        r'Da[:\s]+"?([^"<\n]+)"?\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
        snippet,
    )
    if m:
        return {"name": m.group(1).strip(), "email": m.group(2).strip().lower()}

    # Pattern: Da: Name <email>
    m = re.search(
        r'Da:\s+([^<\n]+)<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>',
        snippet,
    )
    if m:
        return {"name": m.group(1).strip(), "email": m.group(2).strip().lower()}

    return None


# ── HubSpot sync logic (called via MCP tools in the agent loop) ───────────────

HUBSPOT_SOURCE_PROPERTY = "hs_analytics_source"
HUBSPOT_SOURCE_DRILLDOWN = "hs_analytics_source_data_2"
HUBSPOT_SOURCE_VALUE = "EMAIL"
HUBSPOT_SOURCE_DETAIL = "Gmail"

CONTACT_LABEL = "Inbound Gmail"  # used in notes/tags


def build_create_properties(contact: ContactInfo) -> dict:
    props = {
        "email": contact.email,
        HUBSPOT_SOURCE_PROPERTY: HUBSPOT_SOURCE_VALUE,
        HUBSPOT_SOURCE_DRILLDOWN: HUBSPOT_SOURCE_DETAIL,
    }
    if contact.firstname:
        props["firstname"] = contact.firstname
    if contact.lastname:
        props["lastname"] = contact.lastname
    if contact.company:
        props["company"] = contact.company
    return props


def build_update_properties(contact: ContactInfo, existing: dict) -> dict:
    """Return only properties that are missing in the existing HubSpot record."""
    props = {}
    existing_props = existing.get("properties", {})

    for key, value in [
        ("firstname", contact.firstname),
        ("lastname", contact.lastname),
        ("company", contact.company),
    ]:
        if value and not existing_props.get(key):
            props[key] = value

    # Always (re-)set source if not already "EMAIL"
    if existing_props.get(HUBSPOT_SOURCE_PROPERTY) != HUBSPOT_SOURCE_VALUE:
        props[HUBSPOT_SOURCE_PROPERTY] = HUBSPOT_SOURCE_VALUE
        props[HUBSPOT_SOURCE_DRILLDOWN] = HUBSPOT_SOURCE_DETAIL

    return props


# ── Entry point (MCP-driven agent loop) ───────────────────────────────────────

def run_sync_pass(gmail_threads: list, hubspot_results: list) -> list[SyncResult]:
    """
    Pure-Python coordination layer.
    The actual Gmail/HubSpot API calls happen via MCP tools in the agent loop;
    this function orchestrates the contact extraction and dedup logic.

    gmail_threads  : list of thread dicts from mcp__Gmail__search_threads
    hubspot_results: list of contact dicts from mcp__HubSpot__search_crm_objects
    Returns        : list of SyncResult ready to surface to the user
    """
    # Build email → HubSpot record lookup
    hs_by_email = {
        r["properties"]["email"].lower(): r
        for r in hubspot_results
        if r.get("properties", {}).get("email")
    }

    results: list[SyncResult] = []
    seen_emails: set[str] = set()

    for thread in gmail_threads:
        contact = extract_contact(thread)
        if not contact:
            continue

        # Dedup within this batch
        if contact.email in seen_emails:
            continue
        seen_emails.add(contact.email)

        if contact.email in hs_by_email:
            existing = hs_by_email[contact.email]
            update_props = build_update_properties(contact, existing)
            if update_props:
                status = "Aggiornato"
                detail = f"Campi aggiornati: {list(update_props.keys())}"
            else:
                status = "Aggiornato"
                detail = "Già completo"
            results.append(SyncResult(
                status=status,
                email=contact.email,
                hubspot_id=str(existing["id"]),
                detail=detail,
            ))
        else:
            results.append(SyncResult(
                status="Creato",
                email=contact.email,
                hubspot_id="(new)",
                detail=f"Da creare: {contact.firstname} {contact.lastname}".strip(),
            ))

    return results


if __name__ == "__main__":
    # Example: print the sync result table for today's threads
    # (In the agent loop these values come from MCP tool calls)
    print("Gmail → HubSpot sync script loaded. Run via Claude Code scheduled loop.")
