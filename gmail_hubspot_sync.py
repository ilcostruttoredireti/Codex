"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed estrae i mittenti per sincronizzarli in HubSpot.
"""

import re
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Senders to ignore ──────────────────────────────────────────────────────────
IGNORED_DOMAINS = {
    "facebookmail.com",
    "bounce.googlemail.com",
    "noreply.github.com",
    "mailer.twitter.com",
}

IGNORED_PREFIXES = ("noreply", "no-reply", "mailer-daemon", "postmaster", "bounce")

# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class SenderContact:
    email: str
    name: str = ""
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""

    def __post_init__(self):
        if "@" in self.email:
            self.domain = self.email.split("@")[1].lower()
        if self.name and not (self.firstname or self.lastname):
            parts = self.name.strip().split(" ", 1)
            self.firstname = parts[0]
            self.lastname = parts[1] if len(parts) > 1 else ""
        if not self.company and self.domain:
            self.company = domain_to_company(self.domain)


def domain_to_company(domain: str) -> str:
    """Guess company name from email domain (strips TLD and capitalises)."""
    name = domain.split(".")[0]
    overrides = {
        "gmail": "",
        "yahoo": "",
        "hotmail": "",
        "outlook": "",
        "libero": "",
        "virgilio": "",
        "tiscali": "",
    }
    return overrides.get(name, name.replace("-", " ").replace("_", " ").title())


# ── Gmail helpers ───────────────────────────────────────────────────────────────

def parse_sender(raw: str) -> tuple[str, str]:
    """Return (name, email) from a raw 'From' header value."""
    match = re.match(r'"?([^"<]*?)"?\s*<([^>]+)>', raw)
    if match:
        return match.group(1).strip(), match.group(2).strip().lower()
    raw = raw.strip()
    if "@" in raw:
        return "", raw.lower()
    return "", ""


def is_ignored(email: str) -> bool:
    """True if the sender should be skipped."""
    if not email or "@" not in email:
        return True
    local, domain = email.split("@", 1)
    if domain in IGNORED_DOMAINS:
        return True
    if any(local.lower().startswith(p) for p in IGNORED_PREFIXES):
        return True
    return False


def extract_senders_from_threads(threads: list[dict], account_email: str) -> list[SenderContact]:
    """Deduplicate and filter senders from Gmail thread list."""
    seen: set[str] = set()
    contacts: list[SenderContact] = []
    for thread in threads:
        for msg in thread.get("messages", []):
            raw_sender = msg.get("sender", "")
            name, email = parse_sender(raw_sender)
            if not email or email == account_email.lower():
                continue
            if is_ignored(email):
                log.info("IGNORATO (filtro): %s", email)
                continue
            if email in seen:
                continue
            seen.add(email)
            contacts.append(SenderContact(email=email, name=name))
    return contacts


# ── HubSpot helpers (placeholder — chiamate reali passano via MCP) ─────────────

def build_note_body(contact: SenderContact, subjects: list[str]) -> str:
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    lines = [
        f"📧 Email ricevuta via Gmail - {today}",
        "",
        f"Mittente: {contact.email}",
        "Fonte: Gmail (Inbound)",
        "Tag: Inbound Gmail",
    ]
    if subjects:
        lines += ["", f"Email ricevute ({len(subjects)}):"]
        for s in subjects[:10]:
            lines.append(f"  - {s}")
    lines += ["", "Sincronizzato automaticamente da Gmail → HubSpot."]
    return "\n".join(lines)


# ── Sync logic (orchestration pseudo-code) ─────────────────────────────────────

def sync_sender_to_hubspot(
    contact: SenderContact,
    subjects: list[str],
    *,
    hubspot_search_fn,
    hubspot_create_fn,
    hubspot_update_fn,
    hubspot_note_fn,
) -> dict:
    """
    Core sync logic.

    Parameters
    ----------
    contact          : extracted sender info
    subjects         : list of email subjects from this sender
    hubspot_*_fn     : callables injected for testability (or MCP tool wrappers)

    Returns
    -------
    {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "hubspot_id": ...}
    """
    existing = hubspot_search_fn(contact.email)

    if existing:
        hubspot_id = existing["id"]
        props_to_update: dict[str, str] = {}

        existing_props = existing.get("properties", {})
        if not existing_props.get("company") and contact.company:
            props_to_update["company"] = contact.company
        if not existing_props.get("firstname") and contact.firstname:
            props_to_update["firstname"] = contact.firstname
        if not existing_props.get("lastname") and contact.lastname:
            props_to_update["lastname"] = contact.lastname

        if props_to_update:
            hubspot_update_fn(hubspot_id, props_to_update)

        note_body = build_note_body(contact, subjects)
        hubspot_note_fn(hubspot_id, note_body)
        return {"status": "Aggiornato", "email": contact.email, "hubspot_id": hubspot_id}

    # New contact
    props = {
        "email": contact.email,
        "hs_lead_status": "OPEN",
    }
    if contact.firstname:
        props["firstname"] = contact.firstname
    if contact.lastname:
        props["lastname"] = contact.lastname
    if contact.company:
        props["company"] = contact.company

    hubspot_id = hubspot_create_fn(props)
    note_body = build_note_body(contact, subjects)
    hubspot_note_fn(hubspot_id, note_body)
    return {"status": "Creato", "email": contact.email, "hubspot_id": hubspot_id}


def run_sync(threads: list[dict], account_email: str, sync_fns: dict) -> list[dict]:
    """Process all threads and return a result list."""
    contacts = extract_senders_from_threads(threads, account_email)

    # Group subjects by sender email for richer notes
    subjects_by_email: dict[str, list[str]] = {}
    for thread in threads:
        for msg in thread.get("messages", []):
            _, email = parse_sender(msg.get("sender", ""))
            subj = msg.get("subject", "")
            if email and subj:
                subjects_by_email.setdefault(email, []).append(subj)

    results = []
    for contact in contacts:
        try:
            r = sync_sender_to_hubspot(
                contact,
                subjects=subjects_by_email.get(contact.email, []),
                **sync_fns,
            )
            log.info("%-10s | %-40s | ID: %s", r["status"], r["email"], r["hubspot_id"])
            results.append(r)
        except Exception as exc:
            log.error("Errore per %s: %s", contact.email, exc)
            results.append({"status": "Errore", "email": contact.email, "hubspot_id": None})

    return results


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # When run directly, this prints a usage reminder.
    # Real execution happens via the Claude Code MCP session (gmail_hubspot_sync.md).
    print(__doc__)
    print("\nEseguire tramite sessione Claude Code con MCP Gmail + HubSpot abilitati.")
