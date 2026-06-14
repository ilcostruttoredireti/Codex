"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot.

Run this script as a scheduled task (e.g. cron, GitHub Actions).
Requires environment variables:
  GMAIL_MCP_TOKEN  – handled via MCP server (Claude Code on the web)
  HUBSPOT_API_KEY  – handled via MCP server (Claude Code on the web)

When run via Claude Code scheduled routines the MCP servers handle auth
transparently; no manual token management is needed.
"""

import re
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class ContactData:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    source_label: str = "Gmail"

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""

    @property
    def company_from_domain(self) -> str:
        """Derive a readable company name from the email domain."""
        domain = self.domain
        if domain in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it", "virgilio.it"):
            return ""
        # Strip TLD and capitalize
        parts = domain.split(".")
        return parts[0].replace("-", " ").title() if parts else ""


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[int] = None
    note: str = ""


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------

# Matches: "Display Name" <addr@domain> or just addr@domain
_ADDR_RE = re.compile(r'(?:"([^"]+)"\s+)?<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?')
_FORWARD_HEADER_RE = re.compile(
    r'Da\s+"?([^"\n<]+)"?\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})',
    re.IGNORECASE,
)

SKIP_DOMAINS = frozenset({
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "analytics-noreply", "bounce",
})


def _is_noreply(email: str) -> bool:
    local, _, domain = email.partition("@")
    local_lower = local.lower()
    return any(s in local_lower for s in SKIP_DOMAINS) or any(s in domain.lower() for s in SKIP_DOMAINS)


def parse_sender(sender_header: str) -> Optional[ContactData]:
    """Parse a From/sender header into a ContactData (or None if invalid/noreply)."""
    m = _ADDR_RE.search(sender_header)
    if not m:
        return None
    display, email = m.group(1) or "", m.group(2).lower()
    if _is_noreply(email):
        return None

    parts = display.strip().split(None, 1)
    firstname = parts[0] if parts else ""
    lastname = parts[1] if len(parts) > 1 else ""

    cd = ContactData(email=email, firstname=firstname, lastname=lastname)
    cd.company = cd.company_from_domain
    return cd


def extract_forwarded_sender(body: str) -> Optional[ContactData]:
    """
    Extract the original sender from an Italian-style forwarded email body:
      Da "Name" addr@domain
    """
    m = _FORWARD_HEADER_RE.search(body)
    if not m:
        return None
    display, email = m.group(1).strip(), m.group(2).lower()
    if _is_noreply(email):
        return None

    parts = display.split(None, 1)
    firstname = parts[0] if parts else ""
    lastname = parts[1] if len(parts) > 1 else ""

    cd = ContactData(email=email, firstname=firstname, lastname=lastname)
    cd.company = cd.company_from_domain
    return cd


# ---------------------------------------------------------------------------
# HubSpot helpers (thin wrappers – real calls happen via MCP tools)
# ---------------------------------------------------------------------------

def hs_search_contact(email: str, mcp_search_fn) -> Optional[dict]:
    """Return the first HubSpot contact matching *email*, or None."""
    resp = mcp_search_fn(
        objectType="contacts",
        filterGroups=[{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        properties=["email", "firstname", "lastname", "company", "lead_source"],
    )
    results = resp.get("results", [])
    return results[0] if results else None


def hs_create_contact(cd: ContactData, mcp_manage_fn) -> int:
    """Create a new HubSpot contact and return its ID."""
    props = {
        "email": cd.email,
        "lead_source": cd.source_label,
    }
    if cd.firstname:
        props["firstname"] = cd.firstname
    if cd.lastname:
        props["lastname"] = cd.lastname
    if cd.company:
        props["company"] = cd.company

    resp = mcp_manage_fn(
        confirmationStatus="CONFIRMATION_WAIVED_FOR_SESSION",
        createRequest={"objects": [{"objectType": "contacts", "properties": props}]},
    )
    return resp["results"][0]["id"]


def hs_update_contact(contact_id: int, updates: dict, mcp_manage_fn) -> None:
    """Patch missing fields on an existing HubSpot contact."""
    mcp_manage_fn(
        confirmationStatus="CONFIRMATION_WAIVED_FOR_SESSION",
        updateRequest={"objects": [{"objectType": "contacts", "objectId": contact_id, "properties": updates}]},
    )


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def sync_contact(cd: ContactData, mcp_search_fn, mcp_manage_fn) -> SyncResult:
    """Check HubSpot and create/update the contact; return a SyncResult."""
    existing = hs_search_contact(cd.email, mcp_search_fn)

    if existing is None:
        new_id = hs_create_contact(cd, mcp_manage_fn)
        log.info("CREATED  %s  (id=%s)", cd.email, new_id)
        return SyncResult(SyncStatus.CREATED, cd.email, hubspot_id=new_id)

    contact_id = int(existing["id"])
    props = existing.get("properties", {})
    updates: dict = {}

    if not props.get("firstname") and cd.firstname:
        updates["firstname"] = cd.firstname
    if not props.get("lastname") and cd.lastname:
        updates["lastname"] = cd.lastname
    if not props.get("company") and cd.company:
        updates["company"] = cd.company
    if not props.get("lead_source"):
        updates["lead_source"] = cd.source_label

    if updates:
        hs_update_contact(contact_id, updates, mcp_manage_fn)
        log.info("UPDATED  %s  (id=%s) fields=%s", cd.email, contact_id, list(updates))
        return SyncResult(SyncStatus.UPDATED, cd.email, hubspot_id=contact_id, note=str(list(updates)))

    log.info("IGNORED  %s  (id=%s, already complete)", cd.email, contact_id)
    return SyncResult(SyncStatus.IGNORED, cd.email, hubspot_id=contact_id)


def process_threads(threads: list[dict], mcp_search_fn, mcp_manage_fn) -> list[SyncResult]:
    """
    Process a list of Gmail thread objects (as returned by search_threads / get_thread).
    Deduplicates contacts across threads before syncing.
    """
    seen: dict[str, ContactData] = {}

    for thread in threads:
        for msg in thread.get("messages", []):
            sender_raw = msg.get("sender", "")
            body = msg.get("plaintextBody", "")

            # 1. Direct sender
            cd = parse_sender(sender_raw)
            if cd and cd.email not in seen:
                seen[cd.email] = cd

            # 2. Original sender in forwarded email body
            fw = extract_forwarded_sender(body)
            if fw and fw.email not in seen:
                seen[fw.email] = fw

    results = []
    for cd in seen.values():
        results.append(sync_contact(cd, mcp_search_fn, mcp_manage_fn))
    return results


def print_report(results: list[SyncResult]) -> None:
    header = f"{'Stato':<12} {'Email':<50} {'ID HubSpot'}"
    print(header)
    print("-" * len(header))
    for r in results:
        hs_id = str(r.hubspot_id) if r.hubspot_id else "-"
        print(f"{r.status.value:<12} {r.email:<50} {hs_id}")
