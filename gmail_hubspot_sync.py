"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail threads and upserts sender contacts in HubSpot.

Run:
    python gmail_hubspot_sync.py              # one-shot scan
    python gmail_hubspot_sync.py --watch 60   # poll every 60 s
"""

from __future__ import annotations

import argparse
import email.utils
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# MCP tool shims – replaced at runtime by the actual MCP bindings when the
# script is executed inside the Claude Code / MCP environment.  The shims
# are only here so the module can be imported and tested standalone.
# ---------------------------------------------------------------------------
try:
    from mcp_tools import gmail, hubspot  # type: ignore
except ImportError:
    gmail = None      # type: ignore
    hubspot = None    # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

SOURCE_LABEL = "Gmail"
TAG_LABEL = "Inbound Gmail"

IGNORED_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com",
    "outlook.com", "icloud.com", "me.com", "mac.com",
    "live.com", "msn.com", "aol.com",
}


@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""

    @property
    def domain(self) -> str:
        parts = self.email.split("@")
        return parts[1].lower() if len(parts) == 2 else ""

    @property
    def company_from_domain(self) -> str:
        d = self.domain
        if not d or d in IGNORED_DOMAINS:
            return ""
        # "acme.co.uk" → "acme"
        name = d.split(".")[0]
        return name.replace("-", " ").replace("_", " ").title()


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored"
    sender_email: str
    contact_id: str = ""
    detail: str = ""

    def __str__(self) -> str:
        parts = [
            f"Status : {self.status.upper()}",
            f"Email  : {self.sender_email}",
        ]
        if self.contact_id:
            parts.append(f"ID     : {self.contact_id}")
        if self.detail:
            parts.append(f"Detail : {self.detail}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_sender(from_header: str) -> Optional[SenderInfo]:
    """
    Parse a RFC 2822 From header such as:
        "Alice Bob <alice@example.com>"
        "alice@example.com"
    Returns None if no valid email is found.
    """
    name, addr = email.utils.parseaddr(from_header)
    addr = addr.strip().lower()
    if not addr or "@" not in addr:
        return None

    info = SenderInfo(email=addr)

    # Split display name into first / last
    name = name.strip()
    if name:
        parts = name.split(None, 1)
        info.first_name = parts[0]
        info.last_name = parts[1] if len(parts) > 1 else ""

    # Derive company from domain
    info.company = info.company_from_domain
    return info


def extract_message_subject(thread: dict) -> str:
    """Best-effort subject extraction from a Gmail thread dict."""
    try:
        messages = thread.get("messages", [])
        if messages:
            headers = messages[0].get("payload", {}).get("headers", [])
            for h in headers:
                if h.get("name", "").lower() == "subject":
                    return h.get("value", "")
    except Exception:
        pass
    return "(no subject)"


# ---------------------------------------------------------------------------
# HubSpot helpers (via MCP tools)
# ---------------------------------------------------------------------------

def _hs_search_contact(sender_email: str) -> Optional[dict]:
    """Return the first HubSpot contact matching *sender_email*, or None."""
    result = hubspot.search_crm_objects(
        objectType="contacts",
        filterGroups=[
            {
                "filters": [
                    {
                        "propertyName": "email",
                        "operator": "EQ",
                        "value": sender_email,
                    }
                ]
            }
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status", "lifecyclestage"],
        limit=1,
    )
    results = result.get("results", [])
    return results[0] if results else None


def _hs_build_properties(info: SenderInfo, existing: Optional[dict] = None) -> dict:
    """
    Build the HubSpot property map for create or update.
    When *existing* is provided, only return fields that are currently blank.
    """
    props: dict = {}
    existing_props: dict = (existing or {}).get("properties", {})

    def _set(key: str, value: str) -> None:
        if not value:
            return
        if existing and existing_props.get(key):
            return  # already filled in → skip
        props[key] = value

    _set("email", info.email)
    _set("firstname", info.first_name)
    _set("lastname", info.last_name)
    _set("company", info.company)

    # Always stamp the source fields (they may be empty on old records)
    if not existing_props.get("hs_lead_source"):
        props["hs_lead_source"] = SOURCE_LABEL

    return props


def _hs_create_contact(info: SenderInfo) -> str:
    """Create a new HubSpot contact and return its ID."""
    props = _hs_build_properties(info)
    result = hubspot.manage_crm_objects(
        objectType="contacts",
        action="create",
        properties=props,
    )
    return str(result.get("id", ""))


def _hs_update_contact(contact_id: str, info: SenderInfo, existing: dict) -> bool:
    """Update existing contact with any missing properties. Returns True if anything changed."""
    props = _hs_build_properties(info, existing=existing)
    if not props:
        return False
    hubspot.manage_crm_objects(
        objectType="contacts",
        action="update",
        objectId=contact_id,
        properties=props,
    )
    return True


# ---------------------------------------------------------------------------
# Gmail helpers (via MCP tools)
# ---------------------------------------------------------------------------

_PROCESSED_FILE = ".gmail_hubspot_processed.json"


def _load_processed() -> set[str]:
    try:
        with open(_PROCESSED_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_processed(ids: set[str]) -> None:
    with open(_PROCESSED_FILE, "w") as f:
        json.dump(sorted(ids), f)


def fetch_new_threads(processed: set[str], max_results: int = 50) -> list[dict]:
    """
    Return Gmail threads not yet in *processed*.
    Searches for messages in INBOX (unread or recent).
    """
    result = gmail.search_threads(
        query="in:inbox",
        maxResults=max_results,
    )
    threads = result.get("threads", [])
    new_threads = []
    for t in threads:
        tid = t.get("id", "")
        if tid and tid not in processed:
            new_threads.append(t)
    return new_threads


def get_thread_detail(thread_id: str) -> Optional[dict]:
    try:
        return gmail.get_thread(threadId=thread_id)
    except Exception as exc:
        log.warning("Could not fetch thread %s: %s", thread_id, exc)
        return None


def extract_from_header(thread: dict) -> Optional[str]:
    """Return the From header of the first message in a thread."""
    try:
        messages = thread.get("messages", [])
        if not messages:
            return None
        headers = messages[0].get("payload", {}).get("headers", [])
        for h in headers:
            if h.get("name", "").lower() == "from":
                return h.get("value", "")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def process_thread(thread_id: str, thread: dict) -> SyncResult:
    """Process a single Gmail thread and upsert the sender in HubSpot."""
    from_header = extract_from_header(thread)
    if not from_header:
        return SyncResult("ignored", "(unknown)", detail="No From header")

    info = parse_sender(from_header)
    if not info:
        return SyncResult("ignored", from_header, detail="Could not parse sender email")

    # Skip automated senders (noreply, mailer-daemon, etc.)
    if re.search(r"(noreply|no-reply|donotreply|mailer-daemon|bounce)", info.email):
        return SyncResult("ignored", info.email, detail="Automated sender")

    existing = _hs_search_contact(info.email)

    if existing:
        contact_id = str(existing.get("id", ""))
        changed = _hs_update_contact(contact_id, info, existing)
        status = "updated" if changed else "ignored"
        return SyncResult(status, info.email, contact_id=contact_id,
                          detail="Fields updated" if changed else "No new data")
    else:
        contact_id = _hs_create_contact(info)
        return SyncResult("created", info.email, contact_id=contact_id)


def run_sync(max_results: int = 50) -> list[SyncResult]:
    """One full scan: fetch new threads, process each, persist state."""
    processed = _load_processed()
    new_threads = fetch_new_threads(processed, max_results=max_results)

    results: list[SyncResult] = []
    new_ids: set[str] = set()

    for t in new_threads:
        thread_id = t.get("id", "")
        thread_detail = get_thread_detail(thread_id)
        if thread_detail is None:
            continue

        try:
            result = process_thread(thread_id, thread_detail)
        except Exception as exc:
            log.error("Error processing thread %s: %s", thread_id, exc)
            result = SyncResult("ignored", thread_id, detail=str(exc))

        results.append(result)
        new_ids.add(thread_id)

        log.info(
            "[%s] %s  (id=%s)",
            result.status.upper().ljust(7),
            result.sender_email,
            result.contact_id or "-",
        )

    processed.update(new_ids)
    _save_processed(processed)
    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _print_summary(results: list[SyncResult]) -> None:
    if not results:
        print("No new threads found.")
        return

    created = sum(1 for r in results if r.status == "created")
    updated = sum(1 for r in results if r.status == "updated")
    ignored = sum(1 for r in results if r.status == "ignored")

    print(f"\n{'='*55}")
    print(f"  Processed : {len(results)}")
    print(f"  Created   : {created}")
    print(f"  Updated   : {updated}")
    print(f"  Ignored   : {ignored}")
    print(f"{'='*55}")
    for r in results:
        print(f"\n{r}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--watch",
        metavar="SECONDS",
        type=int,
        default=0,
        help="Poll continuously every N seconds (0 = one-shot, default)",
    )
    parser.add_argument(
        "--max-results",
        metavar="N",
        type=int,
        default=50,
        help="Max Gmail threads to fetch per scan (default: 50)",
    )
    args = parser.parse_args(argv)

    if args.watch > 0:
        log.info("Watch mode: scanning every %d s. Press Ctrl+C to stop.", args.watch)
        try:
            while True:
                results = run_sync(max_results=args.max_results)
                _print_summary(results)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            log.info("Stopped.")
    else:
        results = run_sync(max_results=args.max_results)
        _print_summary(results)


if __name__ == "__main__":
    main()
