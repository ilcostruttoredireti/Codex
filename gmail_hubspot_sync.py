"""
Gmail → HubSpot Contact Sync
Scheduled routine: monitors Gmail inbox, extracts senders, syncs to HubSpot.

Execution model: runs via Claude Agent SDK as a scheduled task using MCP tools
for Gmail and HubSpot. This file documents the logic and can also run standalone
via direct API access (requires GMAIL_CREDENTIALS and HUBSPOT_API_KEY env vars).
"""

from __future__ import annotations

import re
import os
import json
import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Emails to always skip (automated / own account)
SKIP_SENDERS: set[str] = {
    "noreply",
    "no-reply",
    "notification",
    "mailer-daemon",
    "postmaster",
}

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"


@dataclass
class ContactData:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""
    source: str = CONTACT_SOURCE

    @classmethod
    def from_sender(cls, sender: str) -> Optional["ContactData"]:
        """Parse sender string like 'John Doe <john@example.com>' or 'john@example.com'."""
        email_match = re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", sender, re.IGNORECASE)
        if not email_match:
            return None

        email = email_match.group(0).lower()
        local, domain = email.rsplit("@", 1)

        if any(skip in local for skip in SKIP_SENDERS):
            return None

        # Extract name from "Name <email>" format
        name_match = re.match(r'^"?([^"<]+)"?\s*<', sender)
        parts = name_match.group(1).strip().split() if name_match else []
        firstname = parts[0] if parts else local.replace(".", " ").replace("_", " ").title()
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""

        company = _company_from_domain(domain)

        return cls(
            email=email,
            firstname=firstname,
            lastname=lastname,
            company=company,
            domain=domain,
        )


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from a domain (strips TLD and common prefixes)."""
    base = domain.split(".")[0]
    skip_prefixes = {"mail", "smtp", "email", "info", "contact"}
    if base in skip_prefixes and len(domain.split(".")) > 2:
        base = domain.split(".")[1]
    return base.replace("-", " ").title()


@dataclass
class SyncResult:
    email: str
    hubspot_id: Optional[str]
    status: str  # "Creato" | "Aggiornato" | "Ignorato"
    note: str = ""


def sync_contacts_mcp(threads: list[dict]) -> list[SyncResult]:
    """
    Core sync logic when executed inside the Claude Agent SDK (MCP mode).
    The actual MCP tool calls happen in the agent wrapper — this function
    documents the algorithm and is used for unit-testing.

    Steps per unique sender:
      1. Parse ContactData from sender string.
      2. Skip bots / own-account emails.
      3. Search HubSpot by email.
      4. If found → patch missing fields only (no-op if complete).
      5. If not found → create with source = "Gmail".
    """
    seen: dict[str, SyncResult] = {}

    for thread in threads:
        for msg in thread.get("messages", []):
            sender = msg.get("sender", "")
            if not sender:
                continue

            contact = ContactData.from_sender(sender)
            if not contact or contact.email in seen:
                continue

            # Placeholder: MCP tool calls happen in agent context
            # mcp__HubSpot__search_crm_objects → check existence
            # mcp__HubSpot__manage_crm_objects → create or patch
            seen[contact.email] = SyncResult(
                email=contact.email,
                hubspot_id=None,
                status="Da elaborare",
            )

    return list(seen.values())


# ---------------------------------------------------------------------------
# Standalone mode (direct API — not needed when running via Claude Agent SDK)
# ---------------------------------------------------------------------------

def _standalone_gmail_search(days: int = 1) -> list[dict]:
    """Fetch inbox threads from Gmail API directly (requires credentials)."""
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(
            os.environ["GMAIL_CREDENTIALS"],
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        svc = build("gmail", "v1", credentials=creds)
        query = f"in:inbox newer_than:{days}d -from:me"
        resp = svc.users().threads().list(userId="me", q=query, maxResults=100).execute()
        return resp.get("threads", [])
    except Exception as exc:
        log.error("Gmail API error: %s", exc)
        return []


def _standalone_hubspot_upsert(contact: ContactData) -> SyncResult:
    """Search and upsert a contact via HubSpot REST API (requires API key)."""
    import urllib.request
    import urllib.error

    api_key = os.environ.get("HUBSPOT_API_KEY", "")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    search_url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    search_payload = json.dumps({
        "filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "EQ", "value": contact.email}
        ]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
    }).encode()

    try:
        req = urllib.request.Request(search_url, data=search_payload, headers=headers, method="POST")
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
    except urllib.error.URLError as exc:
        return SyncResult(email=contact.email, hubspot_id=None, status="Ignorato", note=str(exc))

    results = data.get("results", [])

    props: dict[str, str] = {}
    existing = results[0]["properties"] if results else {}

    if not existing.get("firstname"):
        props["firstname"] = contact.firstname
    if not existing.get("lastname") and contact.lastname:
        props["lastname"] = contact.lastname
    if not existing.get("company"):
        props["company"] = contact.company
    if not existing.get("hs_lead_source"):
        props["hs_lead_source"] = CONTACT_SOURCE

    if results:
        contact_id = results[0]["id"]
        if props:
            patch_url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
            req = urllib.request.Request(
                patch_url,
                data=json.dumps({"properties": props}).encode(),
                headers=headers,
                method="PATCH",
            )
            try:
                with urllib.request.urlopen(req):
                    pass
            except urllib.error.URLError as exc:
                return SyncResult(email=contact.email, hubspot_id=contact_id, status="Ignorato", note=str(exc))
        return SyncResult(email=contact.email, hubspot_id=contact_id, status="Aggiornato")

    # Create new contact
    create_url = "https://api.hubapi.com/crm/v3/objects/contacts"
    create_props = {
        "email": contact.email,
        "firstname": contact.firstname,
        "company": contact.company,
        "hs_lead_source": CONTACT_SOURCE,
    }
    if contact.lastname:
        create_props["lastname"] = contact.lastname

    req = urllib.request.Request(
        create_url,
        data=json.dumps({"properties": create_props}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            created = json.load(resp)
        return SyncResult(email=contact.email, hubspot_id=created["id"], status="Creato")
    except urllib.error.URLError as exc:
        return SyncResult(email=contact.email, hubspot_id=None, status="Ignorato", note=str(exc))


def run_standalone(days: int = 1) -> None:
    threads = _standalone_gmail_search(days)
    if not threads:
        log.info("No new threads found.")
        return

    seen: set[str] = set()
    results: list[SyncResult] = []

    for thread in threads:
        sender_str = thread.get("from", "")
        contact = ContactData.from_sender(sender_str)
        if not contact or contact.email in seen:
            continue
        seen.add(contact.email)
        result = _standalone_hubspot_upsert(contact)
        results.append(result)
        log.info("%-12s | %-40s | %s", result.status, result.email, result.hubspot_id or "—")


if __name__ == "__main__":
    run_standalone()
