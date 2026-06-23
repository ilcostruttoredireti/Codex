"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new inbound emails and syncs senders as HubSpot contacts.
Avoids duplicates using email as unique key. Adds timeline notes tagged "Inbound Gmail".
"""

import re
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Emails to skip (automated senders, own account, notification services)
SKIP_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "accounts.google.com",
    "mail.google.com",
}
SKIP_PREFIXES = ("notification@", "pageupdates@", "noreply@", "no-reply@", "mailer-daemon@")

# The inbox owner's address — never add yourself as a contact
OWN_EMAIL = "cristian.mameli.editore@gmail.com"


@dataclass
class SenderInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""

    @classmethod
    def from_raw(cls, raw_sender: str) -> Optional["SenderInfo"]:
        """Parse 'Name <email>' or plain 'email' strings."""
        raw_sender = raw_sender.strip()
        match = re.match(r"^(.+?)\s*<([^>]+)>$", raw_sender)
        if match:
            name_part = match.group(1).strip().strip('"')
            email = match.group(2).strip().lower()
        else:
            name_part = ""
            email = raw_sender.lower()

        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            return None

        domain = email.split("@", 1)[1]

        if email == OWN_EMAIL:
            return None
        if domain in SKIP_DOMAINS:
            return None
        if any(email.startswith(p) for p in SKIP_PREFIXES):
            return None

        parts = name_part.split() if name_part else []
        firstname = parts[0] if parts else ""
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""

        # Derive company from domain (strip common TLDs, capitalise)
        company = _company_from_domain(domain) if domain not in {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it", "virgilio.it"} else ""

        return cls(email=email, firstname=firstname, lastname=lastname, company=company, domain=domain)


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from email domain."""
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


@dataclass
class SyncResult:
    email: str
    status: str  # "created" | "updated" | "ignored"
    hubspot_id: Optional[int] = None
    reason: str = ""

    def __str__(self) -> str:
        hs = f"HS ID: {self.hubspot_id}" if self.hubspot_id else ""
        return f"[{self.status.upper():8}] {self.email:<40} {hs} {self.reason}"


def _note_body(sender: SenderInfo, email_date: str) -> str:
    return (
        f"\U0001f4e7 Email ricevuta da Gmail - Fonte: Gmail\n"
        f"\U0001f3f7️ Tag: Inbound Gmail\n"
        f"\U0001f4c5 Data email: {email_date}\n"
        f"Mittente: {sender.email}"
    )


# ---------------------------------------------------------------------------
# MCP tool wrappers (called via Claude Code's MCP bridge at runtime)
# These are stubs that document the interface; the actual calls happen through
# the mcp__Gmail__ and mcp__HubSpot__ tools provided by the MCP servers.
# ---------------------------------------------------------------------------

def gmail_search_inbox(page_size: int = 50) -> list[dict]:
    """Return list of thread metadata dicts from Gmail inbox."""
    raise NotImplementedError("Call mcp__Gmail__search_threads in:inbox -from:me")


def hubspot_find_contact(email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    raise NotImplementedError("Call mcp__HubSpot__search_crm_objects CONTACT filter email=EQ")


def hubspot_create_contact(sender: SenderInfo) -> int:
    """Create a new HubSpot contact and return its ID."""
    raise NotImplementedError("Call mcp__HubSpot__manage_crm_objects createRequest contacts")


def hubspot_update_contact(contact_id: int, props: dict) -> None:
    """Patch missing fields on an existing HubSpot contact."""
    raise NotImplementedError("Call mcp__HubSpot__manage_crm_objects updateRequest contacts")


def hubspot_create_note(contact_id: int, body: str, timestamp: str) -> int:
    """Create a timeline note and associate it with a contact."""
    raise NotImplementedError("Call mcp__HubSpot__manage_crm_objects createRequest notes")


# ---------------------------------------------------------------------------
# Core sync logic (pure Python, tool-agnostic)
# ---------------------------------------------------------------------------

def extract_unique_senders(threads: list[dict]) -> list[SenderInfo]:
    """Deduplicate and filter senders from Gmail thread metadata."""
    seen: set[str] = set()
    senders: list[SenderInfo] = []
    for thread in threads:
        for msg in thread.get("messages", []):
            raw = msg.get("sender", "")
            info = SenderInfo.from_raw(raw)
            if info and info.email not in seen:
                seen.add(info.email)
                senders.append(info)
    return senders


def merge_missing_fields(existing: dict, sender: SenderInfo) -> dict:
    """Return only the fields that are empty in HubSpot and known from Gmail."""
    props = existing.get("properties", {})
    updates: dict = {}
    if not props.get("firstname") and sender.firstname:
        updates["firstname"] = sender.firstname
    if not props.get("lastname") and sender.lastname:
        updates["lastname"] = sender.lastname
    if not props.get("company") and sender.company:
        updates["company"] = sender.company
    return updates


def process_senders(senders: list[SenderInfo], email_date: str) -> list[SyncResult]:
    """
    Main sync loop. For each sender:
      - Look up contact in HubSpot
      - Create if missing, update if incomplete
      - Add an Inbound Gmail note to the timeline
    """
    results: list[SyncResult] = []
    ts = datetime.now(timezone.utc).isoformat()

    for sender in senders:
        try:
            existing = hubspot_find_contact(sender.email)

            if existing is None:
                contact_id = hubspot_create_contact(sender)
                hubspot_create_note(contact_id, _note_body(sender, email_date), ts)
                results.append(SyncResult(sender.email, "created", contact_id))

            else:
                contact_id = int(existing["id"])
                updates = merge_missing_fields(existing, sender)
                if updates:
                    hubspot_update_contact(contact_id, updates)
                    hubspot_create_note(contact_id, _note_body(sender, email_date), ts)
                    results.append(SyncResult(sender.email, "updated", contact_id, f"fields: {list(updates)}"))
                else:
                    hubspot_create_note(contact_id, _note_body(sender, email_date), ts)
                    results.append(SyncResult(sender.email, "updated", contact_id, "note added"))

        except Exception as exc:
            log.error("Failed to process %s: %s", sender.email, exc)
            results.append(SyncResult(sender.email, "ignored", reason=str(exc)))

    return results


def run_sync() -> None:
    """Entry point: fetch inbox, process senders, log results."""
    log.info("Starting Gmail → HubSpot sync run")
    threads = gmail_search_inbox()
    senders = extract_unique_senders(threads)
    log.info("Found %d unique external senders", len(senders))

    today = datetime.now(timezone.utc).date().isoformat()
    results = process_senders(senders, today)

    created = [r for r in results if r.status == "created"]
    updated = [r for r in results if r.status == "updated"]
    ignored = [r for r in results if r.status == "ignored"]

    print("\n=== Gmail → HubSpot Sync Results ===")
    for r in results:
        print(r)
    print(f"\nSummary: {len(created)} created, {len(updated)} updated, {len(ignored)} ignored")


if __name__ == "__main__":
    run_sync()
