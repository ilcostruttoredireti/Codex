"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot CRM.
Avoids duplicates (email = chiave unica), updates existing records.
"""

import re
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from email.utils import parseaddr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── domains to skip (notifications, bounces, internal) ──────────────────────
SKIP_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "accounts.google.com",
    "bounce.amazon.com",
    "mailer-daemon.google.com",
}

SKIP_PREFIXES = {"mailer-daemon", "noreply", "no-reply", "notification", "notifications"}

# own addresses — never import the logged-in user as a contact
OWN_EMAILS = {
    "pubblica.latestata@gmail.com",
    "cristian.mameli.editore@gmail.com",
}


@dataclass
class SenderContact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    lead_source: str = "Gmail"
    tag: str = "Inbound Gmail"


@dataclass
class SyncResult:
    email: str
    hubspot_id: Optional[str]
    status: str  # Creato | Aggiornato | Ignorato
    note: str = ""


def _domain(email: str) -> str:
    return email.split("@")[-1].lower() if "@" in email else ""


def _should_skip(email: str) -> bool:
    email = email.lower().strip()
    if email in OWN_EMAILS:
        return True
    dom = _domain(email)
    if dom in SKIP_DOMAINS:
        return True
    local = email.split("@")[0]
    if any(local.startswith(p) for p in SKIP_PREFIXES):
        return True
    return False


def _infer_company_from_domain(email: str) -> str:
    """Best-effort company name from email domain."""
    dom = _domain(email)
    generic = {"gmail.com", "yahoo.com", "hotmail.com", "libero.it",
               "outlook.com", "icloud.com", "pec.it", "legalmail.it"}
    if dom in generic:
        return ""
    # strip TLD (.com .it .org etc.) and capitalise each part
    parts = dom.split(".")
    meaningful = [p for p in parts[:-1] if len(p) > 2]  # skip short TLDs
    if not meaningful:
        return ""
    return " ".join(p.capitalize() for p in meaningful)


def _parse_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last)."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def extract_sender(raw_sender: str, display_name: str = "") -> Optional[SenderContact]:
    """
    Build a SenderContact from a Gmail 'From' header.

    raw_sender  : the bare email address already extracted by Gmail MCP
    display_name: optional display name from the sender field
    """
    email = raw_sender.lower().strip()

    if _should_skip(email):
        return None

    # Try to extract a human name
    firstname, lastname = "", ""
    if display_name:
        # Heuristic: skip strings that look like org names (all-caps words, "ufficio", etc.)
        words = display_name.split()
        looks_like_person = (
            len(words) >= 2
            and all(w[0].isupper() for w in words if w)
            and not any(
                kw in display_name.lower()
                for kw in ("ufficio", "galleria", "press", "redazione",
                           "associazione", "comune", "ministero", "studio",
                           "srl", "spa", "s.r.l", "s.p.a")
            )
        )
        if looks_like_person:
            firstname, lastname = _parse_name(display_name)

    company = _infer_company_from_domain(email)

    return SenderContact(
        email=email,
        firstname=firstname,
        lastname=lastname,
        company=company,
    )


# ── HubSpot helpers (called via MCP tools in the agent loop) ─────────────────

def build_hubspot_properties(contact: SenderContact, existing: Optional[dict] = None) -> dict:
    """
    Return only the properties that need to be written.
    If existing is provided, skip fields that already have a value.
    """
    desired = {
        "email": contact.email,
        "hs_lead_source": "OTHER",          # closest standard value to 'Gmail'
    }
    if contact.firstname:
        desired["firstname"] = contact.firstname
    if contact.lastname:
        desired["lastname"] = contact.lastname
    if contact.company:
        desired["company"] = contact.company

    if existing is None:
        return desired

    # Only write fields that are currently empty in HubSpot
    updates = {}
    for k, v in desired.items():
        if k == "email":
            continue  # never overwrite the key
        current = existing.get(k) or ""
        if not current and v:
            updates[k] = v
    return updates


# ── Core sync logic (pseudo-async; each step calls MCP tools) ────────────────

def parse_gmail_threads(threads: list[dict]) -> list[SenderContact]:
    """
    Extract unique, valid SenderContacts from a list of Gmail thread objects
    (as returned by the mcp__Gmail__search_threads tool).
    """
    seen: set[str] = set()
    contacts: list[SenderContact] = []

    for thread in threads:
        for msg in thread.get("messages", []):
            raw_sender = msg.get("sender", "")
            if not raw_sender:
                continue

            # Gmail MCP returns bare email; try to extract display name too
            if "<" in raw_sender:
                display_name, email = parseaddr(raw_sender)
            else:
                display_name, email = "", raw_sender

            email = email.lower().strip()
            if not email or email in seen:
                continue
            seen.add(email)

            contact = extract_sender(email, display_name)
            if contact:
                contacts.append(contact)

    return contacts


def run_sync(
    gmail_threads: list[dict],
    existing_hubspot_contacts: list[dict],
) -> list[SyncResult]:
    """
    Core sync logic — pure Python, no I/O.

    gmail_threads            : raw output from mcp__Gmail__search_threads
    existing_hubspot_contacts: raw output from mcp__HubSpot__search_crm_objects

    Returns a list of SyncResult with recommended actions.
    The CALLER is responsible for actually creating/updating via MCP tools.
    """
    # Build a lookup: email → hubspot contact
    hs_by_email: dict[str, dict] = {}
    for hs in existing_hubspot_contacts:
        props = hs.get("properties", {})
        em = (props.get("email") or "").lower().strip()
        if em:
            hs_by_email[em] = hs

    sender_contacts = parse_gmail_threads(gmail_threads)
    results: list[SyncResult] = []

    for contact in sender_contacts:
        existing = hs_by_email.get(contact.email)

        if existing is None:
            # → CREA
            results.append(SyncResult(
                email=contact.email,
                hubspot_id=None,
                status="Creato",
                note=f"Nuovo contatto: {contact.firstname} {contact.lastname} ({contact.company})".strip(),
            ))
        else:
            hs_props = existing.get("properties", {})
            updates = build_hubspot_properties(contact, existing=hs_props)
            hs_id = str(existing.get("id", ""))

            if updates:
                results.append(SyncResult(
                    email=contact.email,
                    hubspot_id=hs_id,
                    status="Aggiornato",
                    note=f"Campi aggiornati: {', '.join(updates.keys())}",
                ))
            else:
                results.append(SyncResult(
                    email=contact.email,
                    hubspot_id=hs_id,
                    status="Ignorato",
                    note="Dati già completi",
                ))

    return results


def print_report(results: list[SyncResult]) -> None:
    """Print a formatted sync report."""
    print("\n" + "=" * 65)
    print(f"  GMAIL → HUBSPOT SYNC REPORT — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    print("=" * 65)
    counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0}
    for r in results:
        counts[r.status] += 1
        hs = r.hubspot_id or "—"
        print(f"  [{r.status:10}]  {r.email:<40}  ID: {hs}")
        if r.note:
            print(f"              └─ {r.note}")
    print("-" * 65)
    print(f"  Totale: {len(results)}  |  "
          f"Creati: {counts['Creato']}  |  "
          f"Aggiornati: {counts['Aggiornato']}  |  "
          f"Ignorati: {counts['Ignorato']}")
    print("=" * 65 + "\n")


# ── Entry point when run directly (e.g. via cron or scheduled agent) ─────────

if __name__ == "__main__":
    """
    This script is designed to be called by the Claude Code agent loop.
    In production the agent:
      1. Calls mcp__Gmail__search_threads to fetch inbox emails
      2. Calls mcp__HubSpot__search_crm_objects to find existing contacts
      3. Passes both payloads to run_sync()
      4. Calls mcp__HubSpot__manage_crm_objects to create/update contacts
      5. Calls print_report() and sends a PushNotification with the summary

    For local testing, mock data can be injected here.
    """
    log.info("Gmail → HubSpot sync script loaded. Run via Claude Code agent loop.")
