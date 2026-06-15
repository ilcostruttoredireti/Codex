#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox for inbound senders and syncs them to HubSpot CRM.
- Skips internal/automated senders
- Extracts real senders from forwarded messages
- Checks for duplicates by email (unique key)
- Creates new contacts or updates missing fields on existing ones
- Tags source as "Gmail" / "Inbound Gmail"

Run via Claude Code as a scheduled task.
"""

import re
import json
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional


# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_QUERY = "in:inbox newer_than:7d -from:me"
PAGE_SIZE = 50

INTERNAL_SENDERS = {
    "mailer-daemon@googlemail.com",
    "notification@priority.facebookmail.com",
    "cristian.mameli.editore@gmail.com",
    "pubblica.latestata@gmail.com",
    "redazione@latestata.it",
    "noreply@accounts.google.com",
    "no-reply@accounts.google.com",
    "noreply@google.com",
}

GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "libero.it", "virgilio.it", "tiscali.it",
    "icloud.com", "me.com",
}

LEAD_SOURCE = "Gmail"
LEAD_TAG    = "Inbound Gmail"

# Regex to pull email from snippet forwarded lines like:
#   Da "Name" email@domain.com
#   From: "Name" <email@domain.com>
#   Da email@domain.com
_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
_FORWARD_FROM_RE = re.compile(
    r'(?:Da|From)[:\s]+(?:["\']?(?P<name>[^<"\'\n@]+?)["\']?\s+)?'
    r'<?(?P<email>[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
    re.IGNORECASE,
)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Contact:
    email: str
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    company: Optional[str] = None
    subject: Optional[str] = None

    @property
    def domain(self) -> str:
        return self.email.split("@")[1]

    def auto_company(self) -> Optional[str]:
        if self.domain in GENERIC_DOMAINS:
            return None
        name = self.domain.split(".")[0]
        return name.replace("-", " ").title()

    def hubspot_props(self) -> dict:
        props: dict = {"email": self.email}
        if self.firstname:
            props["firstname"] = self.firstname
        if self.lastname:
            props["lastname"] = self.lastname
        company = self.company or self.auto_company()
        if company:
            props["company"] = company
        return props


@dataclass
class SyncResult:
    email: str
    contact_id: Optional[str]
    status: str      # "CREATO" | "AGGIORNATO" | "IGNORATO"
    notes: str = ""


# ── Extraction helpers ────────────────────────────────────────────────────────

def _parse_name(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Split a raw display name into (firstname, lastname)."""
    raw = raw.strip().strip('"').strip("'").strip()
    if not raw:
        return None, None
    parts = raw.split()
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def extract_sender_from_message(msg: dict) -> Optional[Contact]:
    """
    Return the real external sender from a Gmail message dict.

    For forwarded messages (sender == redazione@latestata.it or similar),
    the real sender is embedded in the snippet as "Da <name> <email>".
    """
    sender: str = msg.get("sender", "")
    snippet: str = msg.get("snippet", "")
    subject: str = msg.get("subject", "")

    # Skip outbound / system / internal
    if not sender or sender in INTERNAL_SENDERS:
        return None
    sender_email = _EMAIL_RE.search(sender)
    if not sender_email:
        return None
    sender_email_str = sender_email.group(0).lower()

    # If sender is an internal forwarder, extract the real sender from snippet
    if sender_email_str in INTERNAL_SENDERS:
        match = _FORWARD_FROM_RE.search(snippet)
        if not match:
            return None
        real_email = match.group("email").lower()
        if real_email in INTERNAL_SENDERS:
            return None
        raw_name = (match.group("name") or "").strip()
        firstname, lastname = _parse_name(raw_name) if raw_name else (None, None)
        return Contact(email=real_email, firstname=firstname,
                       lastname=lastname, subject=subject)

    # Regular external sender
    # Try to parse display name from "Name Surname <email>" format
    name_match = re.match(r'^"?([^<"]+)"?\s*<', sender)
    if name_match:
        raw_name = name_match.group(1).strip()
        firstname, lastname = _parse_name(raw_name)
    else:
        firstname = lastname = None

    return Contact(email=sender_email_str, firstname=firstname,
                   lastname=lastname, subject=subject)


def collect_unique_contacts(threads: list[dict]) -> list[Contact]:
    """
    Walk all messages in all threads, return one Contact per unique email
    (first occurrence wins for name/subject data).
    """
    seen: dict[str, Contact] = {}
    for thread in threads:
        for msg in thread.get("messages", []):
            contact = extract_sender_from_message(msg)
            if contact and contact.email not in seen:
                seen[contact.email] = contact
    return list(seen.values())


# ── HubSpot diff helpers ──────────────────────────────────────────────────────

def fields_to_update(existing_props: dict, candidate: Contact) -> dict:
    """
    Return only the HubSpot properties that are missing / empty on the existing
    contact and that we have data for.
    """
    updates: dict = {}
    candidate_props = candidate.hubspot_props()
    for key, value in candidate_props.items():
        if key == "email":
            continue
        existing_val = existing_props.get(key)
        if not existing_val and value:
            updates[key] = value
    return updates


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(results: list[SyncResult]) -> None:
    created   = [r for r in results if r.status == "CREATO"]
    updated   = [r for r in results if r.status == "AGGIORNATO"]
    skipped   = [r for r in results if r.status == "IGNORATO"]

    print("\n" + "=" * 60)
    print(f"  Gmail → HubSpot Sync  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)
    print(f"  Totale processati : {len(results)}")
    print(f"  ✅ Creati         : {len(created)}")
    print(f"  🔄 Aggiornati     : {len(updated)}")
    print(f"  ⏭  Ignorati       : {len(skipped)}")
    print("-" * 60)

    for label, group in [("CREATI", created), ("AGGIORNATI", updated)]:
        if not group:
            continue
        print(f"\n{label}:")
        for r in group:
            note = f"  ({r.notes})" if r.notes else ""
            print(f"  • {r.email:<45}  ID {r.contact_id}{note}")

    print("=" * 60 + "\n")

    # Machine-readable JSON output
    output = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(results),
            "creati": len(created),
            "aggiornati": len(updated),
            "ignorati": len(skipped),
        },
        "details": [
            {
                "status": r.status,
                "email": r.email,
                "hubspot_id": r.contact_id,
                "notes": r.notes,
            }
            for r in results
        ],
    }
    with open("sync_report.json", "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)
    print("Report salvato in sync_report.json")


# ── Entry point (used when Claude Code runs this directly) ────────────────────

def main():
    """
    This script is designed to be driven by Claude Code's MCP tools
    (mcp__Gmail__ and mcp__HubSpot__).  The functions above implement
    all the pure-Python logic (parsing, diffing, reporting); the actual
    MCP calls happen in the Claude Code session that orchestrates this run.

    Running this file directly prints usage documentation.
    """
    print(__doc__)
    print("Usage: invoke via Claude Code scheduled task.")
    print("       The MCP orchestration calls extract_sender_from_message(),")
    print("       collect_unique_contacts(), fields_to_update(), and print_report().")
    sys.exit(0)


if __name__ == "__main__":
    main()
