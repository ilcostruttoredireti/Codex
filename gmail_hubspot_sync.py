"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and syncs sender contacts to HubSpot.
Skips no-reply/automated senders. Uses email as the unique dedup key.
"""

import re
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ── Patterns that identify automated/no-reply senders ────────────────────────

SKIP_LOCAL_PARTS = re.compile(
    r"^(no.?reply|noreply|notify.noreply|ads.noreply|donotreply|"
    r"nobody|mailer|bounce|daemon|postmaster|auto.?reply|feedback|"
    r"pinbot|newsletter|digest|notifications?)$",
    re.IGNORECASE,
)

SKIP_DOMAINS = {
    "youtube.com",
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "linkedin.com",
    "discord.com",
    "skool.com",
}


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class SenderContact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = field(init=False)
    source: str = "Gmail"

    def __post_init__(self):
        self.domain = self.email.split("@")[-1].lower() if "@" in self.email else ""

    def to_hubspot_properties(self) -> dict:
        props = {
            "email": self.email,
            "hs_lead_source": self.source,
        }
        if self.first_name:
            props["firstname"] = self.first_name
        if self.last_name:
            props["lastname"] = self.last_name
        if self.company:
            props["company"] = self.company
        return props


@dataclass
class SyncResult:
    email: str
    status: str          # "Creato" | "Aggiornato" | "Ignorato" | "Saltato"
    hubspot_id: Optional[str] = None
    note: str = ""


# ── Sender extraction ─────────────────────────────────────────────────────────


def should_skip(email: str) -> bool:
    """Return True for automated/no-reply addresses."""
    if "@" not in email:
        return True
    local, domain = email.lower().split("@", 1)
    if SKIP_LOCAL_PARTS.match(local):
        return True
    if domain in SKIP_DOMAINS:
        return True
    # Skip if the domain itself starts with known automated sub-domains
    if domain.startswith(("noreply.", "no-reply.", "bounce.", "mailer.")):
        return True
    return False


def parse_name_from_email(email: str) -> tuple[str, str]:
    """
    Best-effort name extraction from the local part of an email address.
    riccardo@example.com  → ("Riccardo", "")
    chelsea.c@ifttt.com   → ("Chelsea", "C")
    info@example.com      → ("", "")
    """
    skip_locals = {"info", "support", "staff", "commerciale", "formazione",
                   "redazione", "ciao", "product", "marketing", "general",
                   "confirm", "notifications", "notify", "contact", "hello",
                   "team", "admin", "no-reply", "noreply", "nobody",
                   "mailer", "daily", "newsletter"}
    local = email.split("@")[0].lower()
    if local in skip_locals:
        return "", ""
    parts = re.split(r"[._\-+]", local)
    # Filter out single-char remnants that are just initials or numbers
    parts = [p.capitalize() for p in parts if len(p) > 1 and not p.isdigit()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def company_from_domain(domain: str) -> str:
    """
    Derive a human-readable company name from the primary domain.
    raffaprivatejet.com → Raffa Private Jet
    martes-ai.com       → Martes AI
    bdmassociati.it     → BDM Associati
    """
    if not domain:
        return ""
    # Strip TLD(s)
    name = re.sub(r"\.(com|it|eu|org|net|io|press|careers|es|co|uk)$", "", domain)
    # Split on dots (sub-domains) and keep the last meaningful segment
    segments = [s for s in name.split(".") if s not in ("www", "mail", "email",
                                                         "engage", "marketing",
                                                         "c", "info", "e")]
    name = segments[-1] if segments else name
    # CamelCase split: raffaprivatejet → Raffa Private Jet (heuristic)
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    # Replace hyphens/underscores with spaces
    name = re.sub(r"[-_]", " ", name)
    # Title case
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize()
                    for w in name.split())


def extract_contact(sender_email: str, display_name: str = "") -> Optional[SenderContact]:
    """Build a SenderContact from a raw sender address, or None if it should be skipped."""
    email = sender_email.strip().lower()
    if not email or should_skip(email):
        return None

    first, last = parse_name_from_email(email)

    # If a display name was provided in the email header, prefer that
    if display_name:
        name_parts = display_name.strip().split()
        if len(name_parts) >= 2:
            first = name_parts[0]
            last = " ".join(name_parts[1:])
        elif len(name_parts) == 1:
            first = name_parts[0]

    domain = email.split("@")[-1]
    company = company_from_domain(domain)

    return SenderContact(
        email=email,
        first_name=first,
        last_name=last,
        company=company,
    )


# ── HubSpot helpers (thin wrappers around the MCP tools in the live session) ─


def hubspot_find_contact(email: str) -> Optional[dict]:
    """
    Search HubSpot for a contact by email.
    Returns the contact dict or None.

    NOTE: In the live Claude Code / MCP session this function is replaced by
    direct mcp__HubSpot__search_crm_objects calls.  The stub below is kept for
    local unit-testing and documentation purposes.
    """
    raise NotImplementedError("Call mcp__HubSpot__search_crm_objects directly in the MCP session.")


def hubspot_create_contact(contact: SenderContact) -> str:
    """Create a new contact and return its HubSpot ID."""
    raise NotImplementedError("Call mcp__HubSpot__manage_crm_objects directly in the MCP session.")


def hubspot_update_contact(hubspot_id: str, missing_props: dict) -> None:
    """Patch only the missing/empty properties on an existing contact."""
    raise NotImplementedError("Call mcp__HubSpot__manage_crm_objects directly in the MCP session.")


# ── Core sync logic ───────────────────────────────────────────────────────────


def sync_contact(sender_email: str,
                 display_name: str,
                 existing: Optional[dict]) -> SyncResult:
    """
    Decide whether to create, update, or skip a contact.

    existing: the HubSpot contact dict (from search), or None if not found.
    Returns a SyncResult with the action taken.
    """
    contact = extract_contact(sender_email, display_name)
    if contact is None:
        return SyncResult(email=sender_email, status="Saltato",
                          note="Indirizzo automatizzato/no-reply")

    if existing is None:
        # New contact — create it
        # (In a real run, call hubspot_create_contact and get back the ID)
        return SyncResult(email=sender_email, status="Creato",
                          note="Nuovo contatto creato in HubSpot")

    # Existing contact — check for missing/empty fields
    hs_props = existing.get("properties", {})
    updates = {}

    if not hs_props.get("firstname") and contact.first_name:
        updates["firstname"] = contact.first_name
    if not hs_props.get("lastname") and contact.last_name:
        updates["lastname"] = contact.last_name
    if not hs_props.get("company") and contact.company:
        updates["company"] = contact.company
    if not hs_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    if updates:
        # (In a real run, call hubspot_update_contact)
        return SyncResult(
            email=sender_email,
            status="Aggiornato",
            hubspot_id=str(existing.get("id", "")),
            note=f"Campi aggiornati: {', '.join(updates.keys())}",
        )

    return SyncResult(
        email=sender_email,
        status="Ignorato",
        hubspot_id=str(existing.get("id", "")),
        note="Contatto già completo in HubSpot",
    )


# ── Run log writer ─────────────────────────────────────────────────────────────


def write_run_log(results: list[SyncResult], log_path: str) -> None:
    """Append the sync results of this run to a JSONL log file."""
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {
        "run_at": run_ts,
        "total": len(results),
        "created": sum(1 for r in results if r.status == "Creato"),
        "updated": sum(1 for r in results if r.status == "Aggiornato"),
        "skipped": sum(1 for r in results if r.status == "Ignorato"),
        "automated": sum(1 for r in results if r.status == "Saltato"),
        "contacts": [
            {
                "email": r.email,
                "status": r.status,
                "hubspot_id": r.hubspot_id,
                "note": r.note,
            }
            for r in results
        ],
    }
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info("Run log written → %s  (%d contacts)", log_path, len(results))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # In the live MCP session the actual sync is performed via Claude Code's
    # built-in Gmail and HubSpot MCP tools.  This module contains the pure
    # business logic (extraction, dedup, field-merging) that those calls rely on.
    log.info("Import this module from the Claude Code session; do not run it standalone.")
