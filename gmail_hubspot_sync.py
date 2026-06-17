"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs senders as HubSpot contacts.
"""

import re
import time
import logging
from dataclasses import dataclass
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────

@dataclass
class EmailSender:
    email: str
    first_name: str
    last_name: str
    company: str


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_contact_id: Optional[str]

    def __str__(self) -> str:
        cid = self.hubspot_contact_id or "N/A"
        return f"[{self.status}] {self.email} → HubSpot ID: {cid}"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

_NO_SYNC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "protonmail.com", "noreply",
    "no-reply", "donotreply", "notifications", "mailer-daemon",
}

_NOREPLY_PATTERNS = re.compile(
    r"^(noreply|no-reply|donotreply|mailer-daemon|bounce|postmaster|"
    r"notifications?|support|info|contact|hello|help)@",
    re.IGNORECASE,
)


def _parse_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Handles single-word names."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def _company_from_domain(domain: str) -> str:
    """Convert 'acme.com' → 'Acme'. Strips common TLDs."""
    name = domain.split(".")[0]
    return name.capitalize()


def _extract_sender(from_header: str) -> Optional[EmailSender]:
    """
    Parse a RFC-2822 From header like:
        'Mario Rossi <mario@acme.it>' or 'mario@acme.it'
    Returns None for addresses that should be skipped.
    """
    match = re.match(r'^(?:"?([^"<>]+)"?\s+)?<?([^<>\s]+@[^<>\s>]+)>?$', from_header.strip())
    if not match:
        return None

    display_name = (match.group(1) or "").strip()
    email = match.group(2).strip().lower()

    if _NOREPLY_PATTERNS.match(email):
        return None

    domain = email.split("@")[-1].lower()
    if any(nd in domain for nd in _NO_SYNC_DOMAINS):
        return None

    first_name, last_name = _parse_name(display_name) if display_name else ("", "")
    company = _company_from_domain(domain)

    return EmailSender(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
    )


# ─────────────────────────────────────────────
# Gmail integration (via MCP)
# ─────────────────────────────────────────────

def fetch_recent_threads(mcp, since_hours: int = 1) -> list[dict]:
    """Return Gmail threads received in the last `since_hours` hours."""
    query = f"in:inbox newer_than:{since_hours}h"
    try:
        result = mcp.gmail.search_threads(query=query, max_results=50)
        return result.get("threads", [])
    except Exception as exc:
        logger.error("Gmail search failed: %s", exc)
        return []


def get_thread_sender(mcp, thread_id: str) -> Optional[str]:
    """Extract the From header from the first message of a thread."""
    try:
        thread = mcp.gmail.get_thread(thread_id=thread_id)
        messages = thread.get("messages", [])
        if not messages:
            return None
        headers = messages[0].get("payload", {}).get("headers", [])
        for h in headers:
            if h.get("name", "").lower() == "from":
                return h.get("value")
    except Exception as exc:
        logger.error("Failed to fetch thread %s: %s", thread_id, exc)
    return None


# ─────────────────────────────────────────────
# HubSpot integration (via MCP)
# ─────────────────────────────────────────────

def find_hubspot_contact(mcp, email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns the contact dict or None."""
    try:
        results = mcp.hubspot.search_crm_objects(
            object_type="contacts",
            filter_groups=[{
                "filters": [{
                    "propertyName": "email",
                    "operator": "EQ",
                    "value": email,
                }]
            }],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        )
        hits = results.get("results", [])
        return hits[0] if hits else None
    except Exception as exc:
        logger.error("HubSpot search failed for %s: %s", email, exc)
        return None


def _build_properties(sender: EmailSender, existing: Optional[dict]) -> dict:
    """Build the HubSpot property dict, only filling in blank fields."""
    existing_props = existing.get("properties", {}) if existing else {}

    props: dict = {"leadsource": "Gmail"}

    if not existing_props.get("email"):
        props["email"] = sender.email
    if not existing_props.get("firstname") and sender.first_name:
        props["firstname"] = sender.first_name
    if not existing_props.get("lastname") and sender.last_name:
        props["lastname"] = sender.last_name
    if not existing_props.get("company") and sender.company:
        props["company"] = sender.company

    # Always ensure tag is present
    current_tags = existing_props.get("hs_tag", "") or ""
    if "Inbound Gmail" not in current_tags:
        props["hs_tag"] = (current_tags + ";Inbound Gmail").lstrip(";")

    return props


def create_hubspot_contact(mcp, sender: EmailSender) -> Optional[str]:
    """Create a new HubSpot contact. Returns the new contact ID or None."""
    try:
        properties = {
            "email": sender.email,
            "leadsource": "Gmail",
            "hs_tag": "Inbound Gmail",
        }
        if sender.first_name:
            properties["firstname"] = sender.first_name
        if sender.last_name:
            properties["lastname"] = sender.last_name
        if sender.company:
            properties["company"] = sender.company

        result = mcp.hubspot.manage_crm_objects(
            action="create",
            object_type="contacts",
            properties=properties,
        )
        return result.get("id")
    except Exception as exc:
        logger.error("HubSpot create failed for %s: %s", sender.email, exc)
        return None


def update_hubspot_contact(mcp, contact_id: str, props: dict) -> bool:
    """Update an existing HubSpot contact. Returns True on success."""
    if not props or list(props.keys()) == ["leadsource"]:
        return False  # nothing meaningful to update
    try:
        mcp.hubspot.manage_crm_objects(
            action="update",
            object_type="contacts",
            object_id=contact_id,
            properties=props,
        )
        return True
    except Exception as exc:
        logger.error("HubSpot update failed for ID %s: %s", contact_id, exc)
        return False


def log_activity(mcp, contact_id: str, email: str) -> None:
    """Log an 'Email received' timeline activity on the contact."""
    try:
        mcp.hubspot.manage_crm_objects(
            action="create",
            object_type="engagements",
            properties={
                "engagement": {"type": "EMAIL", "active": True},
                "associations": {"contactIds": [contact_id]},
                "metadata": {
                    "from": {"email": email},
                    "subject": "Inbound Gmail",
                    "direction": "INBOUND",
                },
            },
        )
    except Exception as exc:
        logger.warning("Activity log failed for contact %s: %s", contact_id, exc)


# ─────────────────────────────────────────────
# Core sync logic
# ─────────────────────────────────────────────

def process_thread(mcp, thread_id: str) -> Optional[SyncResult]:
    """
    Process a single Gmail thread:
    1. Extract sender
    2. Check / create / update HubSpot contact
    3. Return SyncResult
    """
    from_header = get_thread_sender(mcp, thread_id)
    if not from_header:
        return None

    sender = _extract_sender(from_header)
    if not sender:
        logger.debug("Skipping non-business sender: %s", from_header)
        return None

    existing = find_hubspot_contact(mcp, sender.email)

    if existing is None:
        contact_id = create_hubspot_contact(mcp, sender)
        if contact_id:
            log_activity(mcp, contact_id, sender.email)
            return SyncResult("Creato", sender.email, contact_id)
        return SyncResult("Ignorato", sender.email, None)

    contact_id = existing["id"]
    props = _build_properties(sender, existing)
    updated = update_hubspot_contact(mcp, contact_id, props)

    if updated:
        log_activity(mcp, contact_id, sender.email)
        return SyncResult("Aggiornato", sender.email, contact_id)

    return SyncResult("Ignorato", sender.email, contact_id)


# ─────────────────────────────────────────────
# Monitoring loop
# ─────────────────────────────────────────────

def run_sync_loop(mcp, poll_interval_seconds: int = 300) -> None:
    """
    Continuously poll Gmail for new emails and sync senders to HubSpot.
    `poll_interval_seconds` defaults to 5 minutes.
    """
    seen_threads: set[str] = set()
    logger.info("Gmail → HubSpot sync started (poll every %ds)", poll_interval_seconds)

    while True:
        threads = fetch_recent_threads(mcp, since_hours=1)
        new_threads = [t for t in threads if t["id"] not in seen_threads]

        if new_threads:
            logger.info("Found %d new thread(s)", len(new_threads))

        for thread in new_threads:
            thread_id = thread["id"]
            seen_threads.add(thread_id)

            result = process_thread(mcp, thread_id)
            if result:
                logger.info("%s", result)

        time.sleep(poll_interval_seconds)


# ─────────────────────────────────────────────
# Entry point (MCP-aware)
# ─────────────────────────────────────────────

def main(mcp) -> None:
    """
    Entry point when invoked from the Claude MCP runtime.
    `mcp` is the injected MCP client with .gmail and .hubspot namespaces.
    """
    run_sync_loop(mcp, poll_interval_seconds=300)


if __name__ == "__main__":
    # Standalone test: run one polling cycle with a mock MCP shim
    import json

    class _MockMCP:
        class gmail:
            @staticmethod
            def search_threads(**_):
                return {"threads": [{"id": "test-001"}]}

            @staticmethod
            def get_thread(thread_id):
                return {
                    "messages": [{
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "Mario Rossi <mario@acme.it>"}
                            ]
                        }
                    }]
                }

        class hubspot:
            @staticmethod
            def search_crm_objects(**_):
                return {"results": []}

            @staticmethod
            def manage_crm_objects(**kwargs):
                print("HubSpot call:", json.dumps(kwargs, indent=2))
                return {"id": "hs-mock-42"}

    mcp = _MockMCP()
    threads = fetch_recent_threads(mcp, since_hours=1)
    for t in threads:
        r = process_thread(mcp, t["id"])
        if r:
            print(r)
