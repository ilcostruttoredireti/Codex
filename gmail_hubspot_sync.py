"""
Gmail → HubSpot Contact Sync

Continuously monitors Gmail for new inbound emails, extracts sender data,
and creates/updates contacts in HubSpot, deduplicating by email address.
"""

import re
import json
import time
import logging
from datetime import datetime, timezone
from email.utils import parseaddr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── helpers ──────────────────────────────────────────────────────────────────

def extract_domain(email_address: str) -> str | None:
    """Return the domain part of an email, or None for common free providers."""
    FREE_DOMAINS = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "live.com", "icloud.com", "me.com", "aol.com", "protonmail.com",
    }
    parts = email_address.lower().split("@")
    if len(parts) != 2:
        return None
    domain = parts[1]
    return None if domain in FREE_DOMAINS else domain


def parse_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (first, last); best-effort."""
    parts = display_name.strip().split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def company_from_domain(domain: str | None) -> str:
    """Convert a domain like acme.io → Acme."""
    if not domain:
        return ""
    base = domain.split(".")[0]
    return base.capitalize()


def safe_get(data: dict, *keys, default=None):
    """Nested dict access with a default."""
    for key in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(key, default)
    return data


# ── core sync logic (uses MCP tools via the agent runtime) ───────────────────

class GmailHubSpotSync:
    """
    Orchestrates polling of Gmail INBOX and upsert of contacts into HubSpot.

    Designed to be driven by the Claude Code agent loop, which has access to
    mcp__Gmail__* and mcp__HubSpot__* tool calls.  The methods below contain
    the business logic; actual MCP invocations are performed by the agent.
    """

    # IDs of threads already processed this session
    _seen_threads: set[str] = set()

    # ── Gmail helpers ─────────────────────────────────────────────────────

    @staticmethod
    def build_gmail_query() -> str:
        return "in:inbox is:unread"

    @staticmethod
    def parse_thread(thread_data: dict) -> list[dict]:
        """
        Extract a list of sender records from a Gmail thread dict as returned
        by mcp__Gmail__get_thread.

        Returns: [{"email": str, "display_name": str, "subject": str,
                   "thread_id": str, "date": str}]
        """
        senders = []
        thread_id = thread_data.get("id", "")
        messages = thread_data.get("messages", [])
        for msg in messages:
            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            from_raw = headers.get("from", "")
            subject = headers.get("subject", "(no subject)")
            date = headers.get("date", "")
            display_name, email_addr = parseaddr(from_raw)
            email_addr = email_addr.lower().strip()
            if not email_addr or "@" not in email_addr:
                continue
            senders.append({
                "email": email_addr,
                "display_name": display_name,
                "subject": subject,
                "thread_id": thread_id,
                "date": date,
            })
        return senders

    # ── HubSpot helpers ───────────────────────────────────────────────────

    @staticmethod
    def build_contact_properties(sender: dict) -> dict:
        """
        Map sender data → HubSpot contact property dict.
        """
        email = sender["email"]
        display_name = sender.get("display_name", "")
        first, last = parse_name(display_name)
        domain = extract_domain(email)
        company = company_from_domain(domain)

        props: dict = {"email": email, "hs_lead_status": "NEW"}
        if first:
            props["firstname"] = first
        if last:
            props["lastname"] = last
        if company:
            props["company"] = company
        props["leadsource"] = "Gmail"
        # Custom property – must exist in the HubSpot portal or the field is ignored
        props["hs_analytics_source"] = "SOCIAL_MEDIA"  # closest built-in source
        return props

    @staticmethod
    def merge_properties(existing: dict, new_props: dict) -> dict:
        """Return only the fields that are missing in the existing contact."""
        return {k: v for k, v in new_props.items() if not existing.get(k)}


# ── agent-facing runner ───────────────────────────────────────────────────────

def process_sender(sender: dict, hs_contact: dict | None) -> dict:
    """
    Pure-logic function: given sender data and an optional existing HubSpot
    contact, return an operation descriptor for the agent to execute.

    Returns:
        {
            "action": "create" | "update" | "skip",
            "email":  str,
            "contact_id": str | None,
            "properties": dict,   # fields to write (empty for skip)
        }
    """
    sync = GmailHubSpotSync()
    new_props = sync.build_contact_properties(sender)

    if hs_contact is None:
        return {
            "action": "create",
            "email": sender["email"],
            "contact_id": None,
            "properties": new_props,
        }

    contact_id = hs_contact.get("id") or hs_contact.get("objectId", "")
    existing_props = hs_contact.get("properties", {})
    missing = sync.merge_properties(existing_props, new_props)

    if not missing:
        return {
            "action": "skip",
            "email": sender["email"],
            "contact_id": contact_id,
            "properties": {},
        }

    return {
        "action": "update",
        "email": sender["email"],
        "contact_id": contact_id,
        "properties": missing,
    }


def format_result(action: str, email: str, contact_id: str | None) -> str:
    status_map = {
        "create": "Creato",
        "update": "Aggiornato",
        "skip":   "Ignorato",
    }
    status = status_map.get(action, action)
    cid = contact_id or "N/A"
    return f"Stato: {status} | Email: {email} | ID HubSpot: {cid}"
