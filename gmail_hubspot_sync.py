"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail and syncs senders as HubSpot contacts.

Usage:
  python gmail_hubspot_sync.py [--days N]

Requires MCP tools: mcp__Gmail__search_threads, mcp__HubSpot__search_crm_objects,
                    mcp__HubSpot__manage_crm_objects
"""

import re
import json
from dataclasses import dataclass, field
from typing import Optional

# Senders to always skip (no-reply patterns, automated platforms)
SKIP_PREFIXES = {"no-reply", "noreply", "notifications", "confirma", "conferma",
                 "conferma-ordine", "conferma-spedizione", "admanager-noreply",
                 "googledev-noreply", "payments-noreply"}

SKIP_DOMAINS = {
    "amazon.it", "amazon.com", "ebay.com", "google.com", "tiktok.com",
    "shop.tiktok.com", "revolut.com", "academia-mail.com", "engage.canva.com",
    "notification.circle.so", "notifications.hubspot.com",
}


@dataclass
class SenderContact:
    email: str
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    company: Optional[str] = None
    domain: str = field(init=False)

    def __post_init__(self):
        self.domain = self.email.split("@")[1] if "@" in self.email else ""

    @property
    def company_from_domain(self) -> str:
        """Derive a readable company name from the email domain."""
        if self.company:
            return self.company
        base = self.domain.split(".")[0]
        return base.replace("-", " ").title()


def parse_sender(sender_str: str) -> tuple[str, Optional[str], Optional[str]]:
    """
    Parse 'Display Name <email>' or raw 'email' into (email, firstname, lastname).
    """
    match = re.match(r"^(.+?)\s*<([^>]+)>$", sender_str.strip())
    if match:
        display_name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
        parts = display_name.split()
        firstname = parts[0] if parts else None
        lastname = " ".join(parts[1:]) if len(parts) > 1 else None
        return email, firstname, lastname
    else:
        email = sender_str.strip().lower()
        return email, None, None


def should_skip(email: str) -> bool:
    """Return True for automated/no-reply addresses that should not become contacts."""
    local, _, domain = email.partition("@")
    if local in SKIP_PREFIXES:
        return True
    if any(email.endswith(f"@{d}") or email.endswith(f".{d}") for d in SKIP_DOMAINS):
        return True
    return False


def extract_contacts_from_threads(threads: list[dict]) -> list[SenderContact]:
    """Deduplicate and parse sender contacts from Gmail thread list."""
    seen: dict[str, SenderContact] = {}
    for thread in threads:
        for msg in thread.get("messages", []):
            raw_sender = msg.get("sender", "")
            if not raw_sender:
                continue
            email, firstname, lastname = parse_sender(raw_sender)
            if not email or should_skip(email):
                continue
            if email not in seen:
                contact = SenderContact(email=email, firstname=firstname, lastname=lastname)
                contact.company = contact.company_from_domain
                seen[email] = contact
    return list(seen.values())


def build_hubspot_properties(contact: SenderContact, existing: Optional[dict] = None) -> dict:
    """Build HubSpot property dict, only setting fields not already present."""
    props: dict = {}
    existing_props = existing.get("properties", {}) if existing else {}

    if not existing_props.get("email"):
        props["email"] = contact.email
    if contact.firstname and not existing_props.get("firstname"):
        props["firstname"] = contact.firstname
    if contact.lastname and not existing_props.get("lastname"):
        props["lastname"] = contact.lastname
    if not existing_props.get("company") and contact.company:
        props["company"] = contact.company
    if not existing_props.get("hs_lead_status"):
        props["hs_lead_status"] = "NEW"
    if not existing_props.get("lifecyclestage"):
        props["lifecyclestage"] = "lead"
    if not existing_props.get("hs_analytics_source_data_1"):
        props["hs_analytics_source_data_1"] = "Gmail"
    return props


# ---------------------------------------------------------------------------
# Sync logic (designed to be called by an orchestration layer / MCP agent)
# ---------------------------------------------------------------------------

def run_sync(threads: list[dict], hubspot_existing: dict[str, dict]) -> list[dict]:
    """
    Core sync function.

    Args:
        threads: raw Gmail thread objects returned by mcp__Gmail__search_threads
        hubspot_existing: dict mapping email → HubSpot contact record

    Returns:
        List of result dicts with keys: status, email, hubspot_id
    """
    contacts = extract_contacts_from_threads(threads)
    results = []

    for contact in contacts:
        existing = hubspot_existing.get(contact.email)
        props = build_hubspot_properties(contact, existing)

        if existing:
            hubspot_id = existing["id"]
            if props:
                # Has fields to update
                action = "AGGIORNATO"
            else:
                action = "IGNORATO"
        else:
            hubspot_id = None
            action = "CREATO"

        results.append({
            "status": action,
            "email": contact.email,
            "hubspot_id": hubspot_id,
            "props_to_write": props,
            "contact": contact,
        })

    return results


if __name__ == "__main__":
    print("This module is designed to be invoked by the Claude MCP agent scheduler.")
    print("Run via: claude-code gmail_hubspot_sync_agent prompt")
