"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs senders as HubSpot contacts.
"""

import re
import json
import time
import logging
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_FILE = Path(".gmail_hubspot_state.json")
POLL_INTERVAL_SECONDS = 60


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""

    @classmethod
    def from_from_header(cls, from_header: str) -> "SenderInfo":
        display_name, email_addr = parseaddr(from_header)
        email_addr = email_addr.strip().lower()

        first_name = ""
        last_name = ""
        if display_name:
            parts = display_name.strip().split()
            first_name = parts[0] if parts else ""
            last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

        company = _company_from_email(email_addr)
        return cls(
            email=email_addr,
            first_name=first_name,
            last_name=last_name,
            company=company,
        )


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "skipped"
    email: str
    contact_id: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "aol.com", "protonmail.com",
    "mail.com", "yandex.com", "gmx.com", "fastmail.com",
}


def _company_from_email(email: str) -> str:
    """Derive a company name from the email domain, skipping personal domains."""
    try:
        domain = email.split("@")[1].lower()
    except IndexError:
        return ""
    if domain in _PERSONAL_DOMAINS:
        return ""
    # Strip TLD and capitalize: acme.io → Acme
    name = domain.split(".")[0]
    return name.capitalize()


def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@]+@[^@]+\.[^@]+$", email))


# ---------------------------------------------------------------------------
# State persistence (tracks processed Gmail message IDs)
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"processed_ids": [], "last_run": None}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Gmail helpers  (uses mcp__Gmail__ tools via the MCP bridge)
# ---------------------------------------------------------------------------

def fetch_new_threads(gmail_tool, processed_ids: set) -> list[dict]:
    """Return threads whose top message hasn't been processed yet."""
    result = gmail_tool.search_threads(query="in:inbox", max_results=50)
    threads = result.get("threads", []) if isinstance(result, dict) else []

    new_threads = []
    for t in threads:
        tid = t.get("id", "")
        if tid and tid not in processed_ids:
            new_threads.append(t)
    return new_threads


def extract_sender_from_thread(gmail_tool, thread_id: str) -> Optional[SenderInfo]:
    """Get the From header of the first message in a thread."""
    try:
        thread = gmail_tool.get_thread(thread_id=thread_id)
    except Exception as exc:
        log.warning("Could not fetch thread %s: %s", thread_id, exc)
        return None

    messages = thread.get("messages", []) if isinstance(thread, dict) else []
    if not messages:
        return None

    first_msg = messages[0]
    headers = first_msg.get("payload", {}).get("headers", [])
    from_header = next(
        (h["value"] for h in headers if h.get("name", "").lower() == "from"),
        None,
    )
    if not from_header:
        return None

    sender = SenderInfo.from_from_header(from_header)
    if not _is_valid_email(sender.email):
        log.debug("Skipping invalid email: %s", sender.email)
        return None
    return sender


# ---------------------------------------------------------------------------
# HubSpot helpers  (uses mcp__HubSpot__ tools via the MCP bridge)
# ---------------------------------------------------------------------------

def find_contact_by_email(hubspot_tool, email: str) -> Optional[dict]:
    """Return the existing HubSpot contact dict or None."""
    try:
        result = hubspot_tool.search_crm_objects(
            object_type="contacts",
            filter_groups=[
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email,
                        }
                    ]
                }
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        results = result.get("results", []) if isinstance(result, dict) else []
        return results[0] if results else None
    except Exception as exc:
        log.warning("HubSpot search failed for %s: %s", email, exc)
        return None


def _build_properties(sender: SenderInfo) -> dict:
    props = {
        "email": sender.email,
        "hs_lead_source": "Gmail",
    }
    if sender.first_name:
        props["firstname"] = sender.first_name
    if sender.last_name:
        props["lastname"] = sender.last_name
    if sender.company:
        props["company"] = sender.company
    return props


def create_contact(hubspot_tool, sender: SenderInfo) -> str:
    """Create a new HubSpot contact; return its ID."""
    props = _build_properties(sender)
    result = hubspot_tool.manage_crm_objects(
        object_type="contacts",
        action="create",
        properties=props,
    )
    contact_id = str(result.get("id", "")) if isinstance(result, dict) else ""
    return contact_id


def update_contact(hubspot_tool, contact_id: str, existing: dict, sender: SenderInfo) -> None:
    """Fill only the fields that are currently empty in HubSpot."""
    existing_props = existing.get("properties", {})
    updates = {}

    if not existing_props.get("firstname") and sender.first_name:
        updates["firstname"] = sender.first_name
    if not existing_props.get("lastname") and sender.last_name:
        updates["lastname"] = sender.last_name
    if not existing_props.get("company") and sender.company:
        updates["company"] = sender.company
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    if updates:
        hubspot_tool.manage_crm_objects(
            object_type="contacts",
            action="update",
            object_id=contact_id,
            properties=updates,
        )


def add_inbound_activity(hubspot_tool, contact_id: str, sender: SenderInfo) -> None:
    """Create a HubSpot note to record the inbound Gmail email."""
    timestamp = datetime.now(timezone.utc).isoformat()
    note_body = (
        f"Inbound Gmail email received from {sender.email} at {timestamp}.\n"
        f"Tag: Inbound Gmail"
    )
    try:
        hubspot_tool.manage_crm_objects(
            object_type="notes",
            action="create",
            properties={
                "hs_note_body": note_body,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            associations=[
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,
                        }
                    ],
                }
            ],
        )
    except Exception as exc:
        log.debug("Could not add note for contact %s: %s", contact_id, exc)


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def process_sender(hubspot_tool, sender: SenderInfo, add_activity: bool) -> SyncResult:
    existing = find_contact_by_email(hubspot_tool, sender.email)

    if existing:
        contact_id = str(existing.get("id", ""))
        update_contact(hubspot_tool, contact_id, existing, sender)
        if add_activity:
            add_inbound_activity(hubspot_tool, contact_id, sender)
        return SyncResult(status="updated", email=sender.email, contact_id=contact_id)
    else:
        try:
            contact_id = create_contact(hubspot_tool, sender)
            if add_activity and contact_id:
                add_inbound_activity(hubspot_tool, contact_id, sender)
            return SyncResult(status="created", email=sender.email, contact_id=contact_id)
        except Exception as exc:
            return SyncResult(
                status="skipped",
                email=sender.email,
                reason=f"HubSpot create failed: {exc}",
            )


def run_sync_cycle(gmail_tool, hubspot_tool, state: dict, add_activity: bool) -> list[SyncResult]:
    processed_ids: set = set(state.get("processed_ids", []))
    results: list[SyncResult] = []

    new_threads = fetch_new_threads(gmail_tool, processed_ids)
    log.info("Found %d new thread(s) to process.", len(new_threads))

    for thread in new_threads:
        thread_id = thread.get("id", "")
        sender = extract_sender_from_thread(gmail_tool, thread_id)

        if sender is None:
            processed_ids.add(thread_id)
            continue

        log.info("Processing sender: %s", sender.email)
        result = process_sender(hubspot_tool, sender, add_activity)
        results.append(result)

        _log_result(result)
        processed_ids.add(thread_id)

    state["processed_ids"] = list(processed_ids)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)
    return results


def _log_result(r: SyncResult) -> None:
    icon = {"created": "+", "updated": "~", "skipped": "-"}.get(r.status, "?")
    if r.status == "skipped":
        log.info("[%s] %s | %s | reason: %s", icon, r.status.upper(), r.email, r.reason)
    else:
        log.info("[%s] %s | %s | HubSpot ID: %s", icon, r.status.upper(), r.email, r.contact_id)


# ---------------------------------------------------------------------------
# MCP tool bridge  (thin wrappers so the core logic stays testable)
# ---------------------------------------------------------------------------

class GmailBridge:
    """Wraps mcp__Gmail__ MCP tools."""

    def __init__(self, mcp_gmail):
        self._g = mcp_gmail

    def search_threads(self, query: str, max_results: int = 50) -> dict:
        return self._g.search_threads(query=query, maxResults=max_results)

    def get_thread(self, thread_id: str) -> dict:
        return self._g.get_thread(threadId=thread_id)


class HubSpotBridge:
    """Wraps mcp__HubSpot__ MCP tools."""

    def __init__(self, mcp_hubspot):
        self._h = mcp_hubspot

    def search_crm_objects(self, **kwargs) -> dict:
        return self._h.search_crm_objects(**kwargs)

    def manage_crm_objects(self, **kwargs) -> dict:
        return self._h.manage_crm_objects(**kwargs)


# ---------------------------------------------------------------------------
# Entry point  (called from the Claude Code agent loop via MCP)
# ---------------------------------------------------------------------------

def main_loop(gmail_tool, hubspot_tool, add_activity: bool = True) -> None:
    """Poll Gmail indefinitely and sync contacts to HubSpot."""
    log.info("Gmail → HubSpot sync started. Poll interval: %ds", POLL_INTERVAL_SECONDS)
    state = _load_state()

    while True:
        try:
            results = run_sync_cycle(gmail_tool, hubspot_tool, state, add_activity)
            if not results:
                log.info("No new contacts to sync.")
        except Exception as exc:
            log.error("Sync cycle failed: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL_SECONDS)


def run_once(gmail_tool, hubspot_tool, add_activity: bool = True) -> list[SyncResult]:
    """Single-pass sync — useful for testing or scheduled invocation."""
    state = _load_state()
    return run_sync_cycle(gmail_tool, hubspot_tool, state, add_activity)


# ---------------------------------------------------------------------------
# CLI shim for local testing without live MCP
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run a single cycle then exit")
    parser.add_argument("--no-activity", action="store_true", help="Skip timeline note creation")
    args = parser.parse_args()

    # In production the MCP tools are injected by the agent framework.
    # For a local dry-run we raise a clear error rather than silently doing nothing.
    raise SystemExit(
        "This script must be called from within a Claude Code agent session "
        "so that the mcp__Gmail__ and mcp__HubSpot__ MCP tools are available.\n"
        "Use the agent entrypoint in agent_entrypoint.py for local testing with mocks."
    )
