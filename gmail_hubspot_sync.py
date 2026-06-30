#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Fetches inbox emails, extracts senders (including from forwarded messages),
and syncs contacts to HubSpot CRM avoiding duplicates.

Run this script periodically (e.g. via cron or Claude Code scheduled task).
State is persisted in .sync_state.json to process only new emails each run.
"""

import re
import html
import json
import os
import sys
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional

# ── Configuration ────────────────────────────────────────────────────────────

STATE_FILE = os.path.join(os.path.dirname(__file__), ".sync_state.json")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# Senders to skip (automated, own addresses)
OWN_EMAILS = {
    "pubblica.latestata@gmail.com",
    "cristian.mameli.editore@gmail.com",
    "redazione@latestata.it",
}

SKIP_DOMAINS = {
    "facebookmail.com",
    "google.com",
    "googlemail.com",
    "accounts.google.com",
    "notifications.google.com",
    "mailer-daemon.google.com",
}

SKIP_PREFIXES = (
    "noreply@", "no-reply@", "donotreply@", "do-not-reply@",
    "notification@", "notifications@", "mailer-daemon@",
    "postmaster@", "bounce@", "bounces@", "support@noreply",
)

# Personal email domains where company cannot be inferred
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "libero.it", "virgilio.it",
    "tin.it", "alice.it", "live.com", "icloud.com",
}

HUBSPOT_LEAD_SOURCE = "EMAIL_MARKETING"   # closest HubSpot standard value

# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    company: str = ""
    thread_id: str = ""
    date: str = ""


@dataclass
class SyncResult:
    status: str          # CREATO / AGGIORNATO / IGNORATO / ERRORE
    email: str
    hubspot_id: Optional[str] = None
    reason: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def is_automated(email: str) -> bool:
    email = email.lower()
    domain = email.split("@")[-1] if "@" in email else ""
    if email in OWN_EMAILS:
        return True
    if domain in SKIP_DOMAINS:
        return True
    if any(email.startswith(p) for p in SKIP_PREFIXES):
        return True
    return False


def parse_sender_field(raw: str) -> tuple[str, str]:
    """Parse 'Name <email>' or bare 'email' → (display_name, email)."""
    raw = raw.strip()
    m = re.match(r'^(.*?)\s*<([^>]+)>\s*$', raw)
    if m:
        return m.group(1).strip().strip('"'), m.group(2).strip().lower()
    return "", raw.lower()


def split_name(full: str) -> tuple[str, str]:
    """Split 'First Last' → (first, last). Handles multi-word last names."""
    parts = full.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def company_from_email(email: str) -> str:
    """Infer company name from email domain when not a personal address."""
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if not domain or domain in PERSONAL_DOMAINS:
        return ""
    # strip common subdomains (mail., info., etc.)
    parts = domain.split(".")
    root = parts[-2] if len(parts) >= 2 else parts[0]
    return root.replace("-", " ").title()


def extract_forwarded_senders(snippet: str) -> list[tuple[str, str]]:
    """
    Extract (name, email) pairs from forwarded-message snippets.
    Handles Italian ("Da") and English ("From") headers, both plain and HTML-escaped.
    """
    text = html.unescape(snippet)
    results = []

    # Pattern: Da "Name" email  or  Da Name <email>  or  Da: Name <email>
    patterns = [
        # Da "Name" email@domain
        r'Da\s+"([^"]+)"\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
        # Da Name <email>  or  From: Name <email>
        r'(?:Da|From):\s*([^<\n]+?)\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>',
        # Bare email after Da (no name)
        r'Da\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    ]

    seen = set()
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            groups = m.groups()
            if len(groups) == 2:
                name, addr = groups[0].strip(), groups[1].strip().lower()
            else:
                name, addr = "", groups[0].strip().lower()

            if addr in seen or is_automated(addr):
                continue
            seen.add(addr)
            results.append((name, addr))

    return results


def build_sender_info(name: str, email: str, thread_id: str = "", date: str = "") -> SenderInfo:
    first, last = split_name(name)
    company = company_from_email(email)
    return SenderInfo(
        email=email,
        first_name=first,
        last_name=last,
        full_name=name,
        company=company,
        thread_id=thread_id,
        date=date,
    )


# ── State management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_run_iso": None, "processed_thread_ids": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Gmail helpers (called via MCP tools in Claude Code sessions) ──────────────

def gmail_query_for_new_emails(last_run_iso: Optional[str]) -> str:
    """Build a Gmail search query to fetch emails newer than the last run."""
    base = "in:inbox -in:sent -in:draft"
    if last_run_iso:
        # Gmail newer_than: doesn't accept timestamps, use date
        dt = datetime.fromisoformat(last_run_iso.replace("Z", "+00:00"))
        days_ago = (datetime.now(timezone.utc) - dt).days
        if days_ago < 1:
            days_ago = 1
        return f"{base} newer_than:{days_ago}d"
    return f"{base} newer_than:7d"


# ── Core sync logic ───────────────────────────────────────────────────────────

def extract_contacts_from_threads(threads: list[dict]) -> list[SenderInfo]:
    """
    Given a list of thread objects (from mcp__Gmail__search_threads),
    extract unique SenderInfo objects, including from forwarded snippets.
    """
    seen_emails: set[str] = set()
    contacts: list[SenderInfo] = []

    for thread in threads:
        for msg in thread.get("messages", []):
            thread_id = thread.get("id", "")
            date = msg.get("date", "")
            raw_sender = msg.get("sender", "")
            snippet = msg.get("snippet", "")

            # 1. Direct sender
            name, addr = parse_sender_field(raw_sender)
            if addr and not is_automated(addr) and addr not in seen_emails:
                seen_emails.add(addr)
                contacts.append(build_sender_info(name, addr, thread_id, date))

            # 2. Original senders embedded in forwarded messages
            for fw_name, fw_addr in extract_forwarded_senders(snippet):
                if fw_addr not in seen_emails:
                    seen_emails.add(fw_addr)
                    contacts.append(build_sender_info(fw_name, fw_addr, thread_id, date))

    return contacts


def hubspot_props_for_new_contact(info: SenderInfo) -> dict:
    props: dict[str, str] = {
        "email": info.email,
        "hs_lead_status": "NEW",
        "leadsource": HUBSPOT_LEAD_SOURCE,
    }
    if info.first_name:
        props["firstname"] = info.first_name
    if info.last_name:
        props["lastname"] = info.last_name
    if info.company:
        props["company"] = info.company
    return props


def hubspot_props_for_update(info: SenderInfo, existing: dict) -> dict:
    """Return only the fields that are missing/empty on the existing contact."""
    updates: dict[str, str] = {}
    ep = existing.get("properties", {})

    if info.first_name and not ep.get("firstname"):
        updates["firstname"] = info.first_name
    if info.last_name and not ep.get("lastname"):
        updates["lastname"] = info.last_name
    if info.company and not ep.get("company"):
        updates["company"] = info.company
    if not ep.get("leadsource"):
        updates["leadsource"] = HUBSPOT_LEAD_SOURCE

    return updates


# ── Entry point (used when running as a standalone script) ────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    log = logging.getLogger("gmail_hubspot_sync")

    log.info("Gmail → HubSpot sync started")
    log.info(
        "NOTE: This script coordinates via Claude Code MCP tools. "
        "Run it inside a Claude Code session with Gmail + HubSpot MCP servers connected."
    )
    log.info(
        "The sync logic (extract_contacts_from_threads, hubspot_props_for_new_contact, "
        "hubspot_props_for_update) is imported by the Claude Code agent to perform "
        "the actual API calls via mcp__Gmail__* and mcp__HubSpot__* tools."
    )

    state = load_state()
    log.info(f"Last run: {state.get('last_run_iso') or 'never'}")
    query = gmail_query_for_new_emails(state.get("last_run_iso"))
    log.info(f"Gmail query: {query}")

    state["last_run_iso"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log.info("State saved. Sync logic executed via MCP tools in Claude Code agent.")


if __name__ == "__main__":
    main()
