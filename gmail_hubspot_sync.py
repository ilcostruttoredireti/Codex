"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and upserts sender contacts into HubSpot.
"""

import re
import time
import json
import logging
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────

@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""

    @classmethod
    def from_header(cls, from_header: str) -> "SenderInfo":
        """Parse a raw From: header into structured sender info."""
        display_name, email_addr = parseaddr(from_header)
        email_addr = email_addr.strip().lower()

        first_name, last_name = _split_name(display_name)
        company = _company_from_email(email_addr)

        return cls(
            email=email_addr,
            first_name=first_name,
            last_name=last_name,
            company=company,
        )


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored"
    email: str
    hubspot_id: Optional[str] = None
    detail: str = ""


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

# Public / generic email domains — do not extract company from these
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "me.com", "aol.com", "protonmail.com", "mail.com",
    "yandex.com", "gmx.com",
}


def _split_name(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _company_from_email(email: str) -> str:
    """Derive a company name from the email domain when not generic."""
    if "@" not in email:
        return ""
    domain = email.split("@", 1)[1].lower()
    if domain in _GENERIC_DOMAINS:
        return ""
    # Strip common TLD suffixes and capitalise
    base = domain.split(".")[0]
    return base.capitalize()


def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


# ──────────────────────────────────────────────
# HubSpot operations  (uses MCP tools at runtime)
# ──────────────────────────────────────────────

def find_hubspot_contact(email: str, mcp_tools) -> Optional[dict]:
    """Return the existing HubSpot contact dict or None."""
    results = mcp_tools.search_crm_objects(
        objectType="contacts",
        filterGroups=[{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
    )
    contacts = results.get("results", [])
    return contacts[0] if contacts else None


def create_hubspot_contact(sender: SenderInfo, mcp_tools) -> str:
    """Create a new HubSpot contact and return its ID."""
    properties = {
        "email": sender.email,
        "hs_lead_source": "Gmail",
    }
    if sender.first_name:
        properties["firstname"] = sender.first_name
    if sender.last_name:
        properties["lastname"] = sender.last_name
    if sender.company:
        properties["company"] = sender.company

    result = mcp_tools.manage_crm_objects(
        createRequest={
            "objects": [{"objectType": "contacts", "properties": properties}]
        }
    )
    return result["results"][0]["id"]


def update_hubspot_contact(contact_id: str, sender: SenderInfo, existing: dict, mcp_tools) -> bool:
    """Fill missing fields on an existing contact. Returns True if any update was made."""
    existing_props = existing.get("properties", {})
    updates = {}

    if sender.first_name and not existing_props.get("firstname"):
        updates["firstname"] = sender.first_name
    if sender.last_name and not existing_props.get("lastname"):
        updates["lastname"] = sender.last_name
    if sender.company and not existing_props.get("company"):
        updates["company"] = sender.company
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    if not updates:
        return False

    mcp_tools.manage_crm_objects(
        updateRequest={
            "objects": [{
                "objectType": "contacts",
                "objectId": int(contact_id),
                "properties": updates,
            }]
        }
    )
    return True


# ──────────────────────────────────────────────
# Core sync logic
# ──────────────────────────────────────────────

def process_email(from_header: str, mcp_tools) -> SyncResult:
    """Main entry point: given a From: header, upsert the contact in HubSpot."""
    sender = SenderInfo.from_header(from_header)

    if not _is_valid_email(sender.email):
        return SyncResult(status="ignored", email=sender.email, detail="invalid email address")

    # Skip system / no-reply addresses
    local = sender.email.split("@")[0].lower()
    if local in {"noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster"}:
        return SyncResult(status="ignored", email=sender.email, detail="system address")

    existing = find_hubspot_contact(sender.email, mcp_tools)

    if existing is None:
        contact_id = create_hubspot_contact(sender, mcp_tools)
        log.info("CREATED  %s  (id=%s)", sender.email, contact_id)
        return SyncResult(status="created", email=sender.email, hubspot_id=contact_id)

    contact_id = existing["id"]
    updated = update_hubspot_contact(contact_id, sender, existing, mcp_tools)
    if updated:
        log.info("UPDATED  %s  (id=%s)", sender.email, contact_id)
        return SyncResult(status="updated", email=sender.email, hubspot_id=contact_id)

    log.info("IGNORED  %s  (id=%s, no new data)", sender.email, contact_id)
    return SyncResult(status="ignored", email=sender.email, hubspot_id=contact_id, detail="no new data")


# ──────────────────────────────────────────────
# Gmail polling loop  (uses MCP tools at runtime)
# ──────────────────────────────────────────────

def poll_gmail(mcp_tools, poll_interval_seconds: int = 60):
    """
    Continuously poll Gmail INBOX for new unread messages and sync senders.
    Keeps track of already-processed message IDs to avoid re-processing.
    """
    seen_ids: set[str] = set()
    log.info("Starting Gmail → HubSpot sync (poll interval: %ds)", poll_interval_seconds)

    while True:
        try:
            messages = mcp_tools.list_messages(
                labelIds=["INBOX", "UNREAD"],
                maxResults=50,
            )

            for msg_meta in messages.get("messages", []):
                msg_id = msg_meta["id"]
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)

                msg = mcp_tools.get_message(userId="me", id=msg_id, format="metadata",
                                             metadataHeaders=["From", "Subject"])
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                from_header = headers.get("From", "")
                subject = headers.get("Subject", "(no subject)")

                if not from_header:
                    continue

                log.info("Processing email: From=%s | Subject=%s", from_header, subject)
                result = process_email(from_header, mcp_tools)
                _print_result(result)

        except Exception as exc:
            log.error("Error during poll cycle: %s", exc)

        time.sleep(poll_interval_seconds)


def _print_result(result: SyncResult):
    status_upper = result.status.upper().ljust(8)
    detail = f"  ({result.detail})" if result.detail else ""
    hs_id = f"  HubSpot ID: {result.hubspot_id}" if result.hubspot_id else ""
    print(f"[{status_upper}]  {result.email}{hs_id}{detail}")


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--interval", type=int, default=60,
                        help="Poll interval in seconds (default: 60)")
    parser.add_argument("--once", action="store_true",
                        help="Run a single poll cycle then exit (useful for testing)")
    args = parser.parse_args()

    # When running standalone the MCP tools are injected via the Claude Code
    # agent runtime. In unit tests, pass a mock object with the same interface.
    try:
        from mcp_runtime import get_tools   # type: ignore
        tools = get_tools()
    except ImportError:
        print("MCP runtime not available — run inside a Claude Code agent session.")
        raise SystemExit(1)

    if args.once:
        messages = tools.list_messages(labelIds=["INBOX", "UNREAD"], maxResults=50)
        for msg_meta in messages.get("messages", []):
            msg = tools.get_message(userId="me", id=msg_meta["id"], format="metadata",
                                     metadataHeaders=["From", "Subject"])
            headers = {h["name"]: h["value"]
                       for h in msg.get("payload", {}).get("headers", [])}
            from_header = headers.get("From", "")
            if from_header:
                result = process_email(from_header, tools)
                _print_result(result)
    else:
        poll_gmail(tools, poll_interval_seconds=args.interval)
