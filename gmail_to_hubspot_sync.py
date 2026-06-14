"""
Gmail → HubSpot Contact Sync
Scans incoming Gmail threads and syncs senders as HubSpot contacts.
Avoids duplicates using email as unique key; updates existing records
only when fields are missing.
"""

import re
import json
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

# ─── Data model ─────────────────────────────────────────────────────────────

@dataclass
class Sender:
    email: str
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    company: Optional[str] = None
    source_thread_id: Optional[str] = None

@dataclass
class SyncResult:
    email: str
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    hubspot_id: Optional[str] = None
    detail: str = ""

# ─── Helpers ────────────────────────────────────────────────────────────────

# Domains to skip (noise, system addresses, own domain, social networks)
SKIP_DOMAINS = {
    "facebookmail.com", "googlemail.com", "google.com",
    "latestata.it",      # internal editorial address
}

SKIP_EMAILS = {
    "mailer-daemon@googlemail.com",
    "cristian.mameli.editore@gmail.com",
    "pubblica.latestata@gmail.com",
    "redazione@latestata.it",
}

_FROM_RE = re.compile(
    r'Da\s+(?:&quot;([^&]+)&quot;\s+)?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)
_NAME_QUOTED_RE = re.compile(r'"([^"]+)"\s+<([^>]+)>')


def _domain(email: str) -> str:
    return email.split("@", 1)[-1].lower()


def _company_from_domain(email: str) -> str:
    """Best-effort company name from domain (strips TLD and common prefixes)."""
    dom = _domain(email)
    parts = dom.rsplit(".", 2)
    core = parts[-2] if len(parts) >= 2 else parts[0]
    return core.replace("-", " ").replace("_", " ").title()


def _split_name(full: str) -> tuple[str, str]:
    """Split 'First Last' → ('First', 'Last'); handles single names."""
    parts = full.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], ""


def extract_sender(thread: dict) -> Optional[Sender]:
    """
    Pull the primary inbound sender from a Gmail thread dict.
    For forwarded emails (redazione@latestata.it), parse the original
    sender from the snippet.
    """
    messages = thread.get("messages", [])
    if not messages:
        return None

    # Use the first message that has an INBOX label
    msg = next(
        (m for m in messages if "INBOX" in m.get("labelIds", [])),
        messages[0],
    )

    raw_sender = msg.get("sender", "")
    snippet = msg.get("snippet", "")

    # Determine effective sender
    if not raw_sender or raw_sender in SKIP_EMAILS or _domain(raw_sender) in SKIP_DOMAINS:
        # Try to parse original sender from forwarded snippet
        m = _FROM_RE.search(snippet)
        if not m:
            return None
        name_raw = m.group(1) or ""
        email = m.group(2).lower()
    else:
        email = raw_sender.lower()
        name_raw = ""

    if email in SKIP_EMAILS or _domain(email) in SKIP_DOMAINS:
        return None

    # Extract name components
    firstname, lastname = "", ""
    if name_raw:
        name_clean = name_raw.strip().strip('"')
        firstname, lastname = _split_name(name_clean)

    company = _company_from_domain(email)

    return Sender(
        email=email,
        firstname=firstname or None,
        lastname=lastname or None,
        company=company,
        source_thread_id=thread.get("id"),
    )


def deduplicate(senders: list[Sender]) -> list[Sender]:
    seen: dict[str, Sender] = {}
    for s in senders:
        if s.email not in seen:
            seen[s.email] = s
        else:
            # Enrich existing with any extra data
            existing = seen[s.email]
            if not existing.firstname and s.firstname:
                existing.firstname = s.firstname
            if not existing.lastname and s.lastname:
                existing.lastname = s.lastname
    return list(seen.values())


# ─── HubSpot helpers (called by the orchestrator) ────────────────────────────

def build_contact_properties(sender: Sender) -> dict:
    props: dict[str, str] = {"email": sender.email}
    if sender.firstname:
        props["firstname"] = sender.firstname
    if sender.lastname:
        props["lastname"] = sender.lastname
    if sender.company:
        props["company"] = sender.company
    props["hs_lead_source"] = "OTHER"   # nearest standard value to "Gmail"
    props["lead_source_detail"] = "Inbound Gmail"
    return props


def needs_update(existing_props: dict, sender: Sender) -> dict:
    """Return only the fields that are missing in HubSpot and available locally."""
    updates: dict[str, str] = {}
    if not existing_props.get("firstname") and sender.firstname:
        updates["firstname"] = sender.firstname
    if not existing_props.get("lastname") and sender.lastname:
        updates["lastname"] = sender.lastname
    if not existing_props.get("company") and sender.company:
        updates["company"] = sender.company
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "OTHER"
    return updates


# ─── Main orchestration logic ─────────────────────────────────────────────────

def run_sync(threads: list[dict], existing_contacts: list[dict]) -> list[SyncResult]:
    """
    Core sync logic (pure Python, independent of MCP transport).

    Args:
        threads: raw Gmail thread objects
        existing_contacts: HubSpot contact objects already fetched

    Returns:
        list of SyncResult, one per unique sender
    """
    # 1. Extract senders
    raw_senders = [s for t in threads if (s := extract_sender(t)) is not None]
    senders = deduplicate(raw_senders)

    # 2. Build lookup of existing HubSpot contacts by email
    hs_by_email: dict[str, dict] = {}
    for c in existing_contacts:
        email = (c.get("properties") or {}).get("email", "").lower()
        if email:
            hs_by_email[email] = c

    results: list[SyncResult] = []
    new_contacts: list[Sender] = []
    update_contacts: list[tuple[str, dict, Sender]] = []  # (hs_id, updates, sender)

    # 3. Classify
    for sender in senders:
        existing = hs_by_email.get(sender.email)
        if existing is None:
            new_contacts.append(sender)
        else:
            updates = needs_update(existing.get("properties", {}), sender)
            hs_id = existing["id"]
            if updates:
                update_contacts.append((hs_id, updates, sender))
            else:
                results.append(SyncResult(
                    email=sender.email,
                    status="Ignorato",
                    hubspot_id=str(hs_id),
                    detail="già completo",
                ))

    # Return structured plan (actual HubSpot writes happen via MCP tools outside)
    return results, new_contacts, update_contacts


# ─── CLI entry-point (for local testing / documentation) ─────────────────────

if __name__ == "__main__":
    print("Gmail → HubSpot sync module loaded.")
    print("Run via the MCP orchestration harness — see README.")
