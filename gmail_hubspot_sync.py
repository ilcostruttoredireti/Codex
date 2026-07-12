"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail, extracts senders, creates/updates HubSpot contacts.
Designed to run as a scheduled Claude Code automation via MCP tools.
"""

import re
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Email prefixes that indicate automated/system senders – skip these
AUTOMATED_PREFIXES = {
    "no-reply", "noreply", "do-not-reply", "donotreply",
    "mailer-daemon", "postmaster", "daemon", "bounce",
    "notifications", "notify", "notify-noreply",
    "ads-noreply", "admanager-noreply", "payments-noreply",
    "messaging-digest-noreply", "noreply-accounts",
    "noreply-googleworkspacereferral", "nobody", "pinbot",
    "dailybriefing",  # automated newsletters
}

# Prefixes that are generic but may belong to real companies (keep them)
GENERIC_BUT_KEEP = {
    "info", "support", "commerciale", "formazione", "redazione",
    "staff", "team", "ciao", "hello", "hi", "general", "contact",
    "marketing", "product", "publishing",
}

# Domain suffixes for company name inference
COMMON_TLDS = {".com", ".it", ".eu", ".org", ".net", ".io", ".ai",
               ".co", ".us", ".de", ".fr", ".es", ".press", ".careers"}


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SenderInfo:
    email: str
    prefix: str
    domain: str
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    company: Optional[str] = None


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Sender parsing helpers
# ---------------------------------------------------------------------------

def parse_sender(sender_email: str) -> Optional[SenderInfo]:
    """
    Parse a raw sender email into structured contact data.
    Returns None if the sender should be skipped (automated).
    """
    email = sender_email.strip().lower()
    if "@" not in email:
        return None

    prefix, domain = email.split("@", 1)

    # Skip automated senders
    clean_prefix = prefix.replace("-", "").replace(".", "").replace("_", "")
    for auto in AUTOMATED_PREFIXES:
        if prefix == auto or clean_prefix == auto.replace("-", "").replace(".", ""):
            return None

    # Infer firstname from prefix when it looks like a real name
    firstname, lastname = _infer_name(prefix)

    # Infer company from domain
    company = _domain_to_company(domain)

    return SenderInfo(
        email=email,
        prefix=prefix,
        domain=domain,
        firstname=firstname,
        lastname=lastname,
        company=company,
    )


def _infer_name(prefix: str) -> tuple[Optional[str], Optional[str]]:
    """
    Try to extract a human name from an email prefix.
    e.g. "riccardo" → ("Riccardo", None)
         "chelsea.c" → ("Chelsea", "C")
         "info"      → ("Info", None)   # generic, but kept for HubSpot
    """
    # firstname.lastname or firstname_lastname patterns
    if "." in prefix:
        parts = prefix.split(".")
        firstname = parts[0].title()
        lastname = parts[1].title() if len(parts) > 1 else None
        return firstname, lastname
    if "_" in prefix:
        parts = prefix.split("_")
        firstname = parts[0].title()
        lastname = parts[1].title() if len(parts) > 1 else None
        return firstname, lastname

    # Single word – title-case it
    return prefix.title(), None


def _domain_to_company(domain: str) -> str:
    """
    Convert an email domain to a human-readable company name.
    e.g. "martes-ai.com" → "Martes AI"
         "rizzoaiacademy.com" → "Rizzoaiacademy"
    """
    # Remove known TLDs iteratively
    name = domain
    for tld in sorted(COMMON_TLDS, key=len, reverse=True):
        if name.endswith(tld):
            name = name[: -len(tld)]
            break

    # Remove subdomains that are clearly not brand names
    parts = name.split(".")
    # Take the last part (SLD) as the primary brand
    sld = parts[-1]

    # Replace separators with spaces and title-case
    sld = sld.replace("-", " ").replace("_", " ")

    # Uppercase known abbreviations
    words = []
    for word in sld.split():
        if word.lower() in {"ai", "seo", "crm", "erp", "wp", "api", "it", "eu"}:
            words.append(word.upper())
        else:
            words.append(word.title())

    return " ".join(words) if words else domain


# ---------------------------------------------------------------------------
# HubSpot helpers (for use as documentation; MCP calls made by Claude)
# ---------------------------------------------------------------------------

def build_hubspot_properties(sender: SenderInfo) -> dict:
    """
    Build the HubSpot contact property dict for create/update operations.
    """
    props: dict = {
        "email": sender.email,
        "leadsource": "OTHER",          # closest built-in; see note below
        "hs_lead_source_data_1": "Gmail",  # free-text field for source detail
    }
    if sender.firstname:
        props["firstname"] = sender.firstname
    if sender.lastname:
        props["lastname"] = sender.lastname
    if sender.company:
        props["company"] = sender.company
    return props


# ---------------------------------------------------------------------------
# Main sync logic (pseudocode – actual API calls made via MCP in Claude loop)
# ---------------------------------------------------------------------------

def sync_email_senders(threads: list[dict]) -> list[SyncResult]:
    """
    Process a list of Gmail thread objects (as returned by search_threads MCP tool).
    Returns a SyncResult for every processed sender.

    In the Claude Code scheduled session this function's logic is executed
    step-by-step via MCP tool calls (mcp__Gmail__search_threads,
    mcp__HubSpot__search_crm_objects, mcp__HubSpot__manage_crm_objects).
    """
    results: list[SyncResult] = []
    seen_emails: set[str] = set()

    for thread in threads:
        for message in thread.get("messages", []):
            raw_sender = message.get("sender", "")
            if not raw_sender or raw_sender in seen_emails:
                continue
            seen_emails.add(raw_sender)

            sender = parse_sender(raw_sender)
            if sender is None:
                results.append(SyncResult(
                    status=SyncStatus.IGNORED,
                    email=raw_sender,
                    reason="Automated/system sender skipped",
                ))
                continue

            # --- MCP call: search HubSpot by email ---
            # existing = mcp__HubSpot__search_crm_objects(
            #     objectType="CONTACT",
            #     filterGroups=[{"filters": [{"propertyName": "email",
            #                                 "operator": "EQ",
            #                                 "value": sender.email}]}],
            #     properties=["email","firstname","lastname","company","leadsource"],
            # )
            # if existing["total"] > 0:
            #     contact = existing["results"][0]
            #     props = build_missing_fields(contact, sender)
            #     if props:
            #         mcp__HubSpot__manage_crm_objects(updateRequest={...})
            #         status = SyncStatus.UPDATED
            #     else:
            #         status = SyncStatus.IGNORED (already complete)
            # else:
            #     mcp__HubSpot__manage_crm_objects(createRequest={...})
            #     status = SyncStatus.CREATED

            results.append(SyncResult(
                status=SyncStatus.CREATED,   # placeholder
                email=sender.email,
            ))

    return results


def build_missing_fields(existing: dict, sender: SenderInfo) -> dict:
    """
    Return only the HubSpot properties that are currently empty on the contact.
    Used to update without overwriting existing data.
    """
    props = existing.get("properties", {})
    updates: dict = {}

    if not props.get("firstname") and sender.firstname:
        updates["firstname"] = sender.firstname
    if not props.get("lastname") and sender.lastname:
        updates["lastname"] = sender.lastname
    if not props.get("company") and sender.company:
        updates["company"] = sender.company
    if not props.get("leadsource"):
        updates["leadsource"] = "OTHER"
        updates["hs_lead_source_data_1"] = "Gmail"

    return updates


# ---------------------------------------------------------------------------
# Entry point (for standalone use with real Gmail/HubSpot API credentials)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # When running standalone, replace with actual Gmail API + HubSpot API calls.
    # In the Claude Code scheduled environment, Claude executes the MCP calls
    # directly (see session instructions in CLAUDE.md).
    print("Gmail → HubSpot sync module loaded.")
    print("Run via Claude Code scheduled session or provide API credentials.")
