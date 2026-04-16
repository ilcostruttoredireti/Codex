"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail threads and creates/updates HubSpot contacts.
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
from enum import Enum

import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

GMAIL_TOKEN = os.environ["GMAIL_ACCESS_TOKEN"]
HUBSPOT_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
HUBSPOT_BASE = "https://api.hubapi.com"

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "5"))
STATE_FILE = os.getenv("STATE_FILE", ".sync_state.json")


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str]
    thread_id: str


# ─── State helpers ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_thread_ids": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── Gmail helpers ────────────────────────────────────────────────────────────

def _gmail_headers(token: str = GMAIL_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def list_inbox_threads(since_minutes: int = LOOKBACK_MINUTES) -> list[dict]:
    after = (datetime.utcnow() - timedelta(minutes=since_minutes)).strftime("%Y/%m/%d")
    params = {
        "q": f"in:inbox after:{after}",
        "maxResults": 50,
    }
    resp = httpx.get(f"{GMAIL_BASE}/threads", headers=_gmail_headers(), params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("threads", [])


def get_thread_sender(thread_id: str) -> Optional[dict]:
    """Return {email, first_name, last_name, display_name} from the first message of a thread."""
    resp = httpx.get(
        f"{GMAIL_BASE}/threads/{thread_id}",
        headers=_gmail_headers(),
        params={"format": "metadata", "metadataHeaders": "From"},
        timeout=15,
    )
    resp.raise_for_status()
    messages = resp.json().get("messages", [])
    if not messages:
        return None

    headers = messages[0].get("payload", {}).get("headers", [])
    from_header = next((h["value"] for h in headers if h["name"] == "From"), None)
    if not from_header:
        return None

    return parse_from_header(from_header)


def parse_from_header(from_value: str) -> dict:
    """
    Parse RFC 5322 From header.
    Examples:
      "John Doe <john.doe@example.com>"
      "john.doe@example.com"
    """
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', from_value.strip())
    if match:
        display_name = match.group(1).strip()
        email = match.group(2).strip().lower()
    else:
        display_name = ""
        email = from_value.strip().lower()

    parts = display_name.split(None, 1)
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""

    domain = email.split("@")[-1] if "@" in email else ""
    company = _domain_to_company(domain)

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "display_name": display_name,
        "domain": domain,
        "company": company,
    }


def _domain_to_company(domain: str) -> str:
    """Best-effort company name from domain (strips TLD and capitalises)."""
    generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
               "live.com", "me.com", "aol.com", "protonmail.com", "libero.it",
               "tin.it", "alice.it", "virgilio.it"}
    if domain in generic:
        return ""
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


# ─── HubSpot helpers ──────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def find_contact_by_email(email: str) -> Optional[dict]:
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    resp = httpx.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(sender: dict) -> dict:
    properties = _build_properties(sender)
    resp = httpx.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": properties},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def update_contact(contact_id: str, sender: dict, existing_props: dict) -> dict:
    """Only patch fields that are missing in HubSpot."""
    updates = {}
    if not existing_props.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not existing_props.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not existing_props.get("company") and sender["company"]:
        updates["company"] = sender["company"]

    if not updates:
        return {}

    resp = httpx.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _build_properties(sender: dict) -> dict:
    props = {
        "email": sender["email"],
        "hs_lead_status": "NEW",
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]
    return props


def add_inbound_tag(contact_id: str) -> None:
    """Apply 'Inbound Gmail' via a custom hs_tag property (list-type)."""
    # HubSpot stores tags as semicolon-separated values in hs_additional_emails
    # or a custom multi-checkbox. We use a simple note-style activity instead.
    payload = {
        "properties": {
            "hs_content_membership_notes": "Inbound Gmail",
        }
    }
    httpx.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )


def log_email_activity(contact_id: str, sender: dict) -> None:
    """Create a timeline note: email received."""
    payload = {
        "properties": {
            "hs_timestamp": str(int(datetime.utcnow().timestamp() * 1000)),
            "hs_note_body": (
                f"Email inbound da Gmail ricevuta da {sender['display_name']} "
                f"<{sender['email']}> il {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}. "
                f"Tag: Inbound Gmail"
            ),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    }
    try:
        resp = httpx.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/notes",
            headers=_hs_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Could not log timeline activity: %s", exc)


# ─── Core sync logic ──────────────────────────────────────────────────────────

def process_thread(thread_id: str) -> Optional[SyncResult]:
    sender = get_thread_sender(thread_id)
    if not sender or not sender["email"]:
        return None

    email = sender["email"]

    # Skip if own address or no-reply
    if email.startswith("no-reply") or email.startswith("noreply") or email.startswith("donotreply"):
        return SyncResult(SyncStatus.IGNORED, email, None, thread_id)

    existing = find_contact_by_email(email)

    if existing:
        contact_id = existing["id"]
        update_contact(contact_id, sender, existing.get("properties", {}))
        log_email_activity(contact_id, sender)
        return SyncResult(SyncStatus.UPDATED, email, contact_id, thread_id)
    else:
        created = create_contact(sender)
        contact_id = created["id"]
        add_inbound_tag(contact_id)
        log_email_activity(contact_id, sender)
        return SyncResult(SyncStatus.CREATED, email, contact_id, thread_id)


def run_once() -> list[SyncResult]:
    state = load_state()
    processed_ids: set = set(state.get("processed_thread_ids", []))

    threads = list_inbox_threads()
    log.info("Found %d threads in inbox (last %d min)", len(threads), LOOKBACK_MINUTES)

    results: list[SyncResult] = []
    new_ids: list[str] = []

    for thread in threads:
        tid = thread["id"]
        if tid in processed_ids:
            log.debug("Thread %s already processed, skipping.", tid)
            continue

        try:
            result = process_thread(tid)
            if result:
                results.append(result)
                log.info(
                    "%-10s | %-40s | HubSpot ID: %s",
                    result.status.value,
                    result.email,
                    result.hubspot_id or "—",
                )
        except Exception as exc:
            log.error("Error processing thread %s: %s", tid, exc)

        new_ids.append(tid)

    # Persist only last 5000 thread IDs to avoid unbounded growth
    all_ids = list(processed_ids) + new_ids
    state["processed_thread_ids"] = all_ids[-5000:]
    save_state(state)

    return results


def run_continuous() -> None:
    log.info("Starting continuous Gmail → HubSpot sync (interval: %ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            results = run_once()
            created = sum(1 for r in results if r.status == SyncStatus.CREATED)
            updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
            ignored = sum(1 for r in results if r.status == SyncStatus.IGNORED)
            log.info("Cycle done — Creati: %d | Aggiornati: %d | Ignorati: %d", created, updated, ignored)
        except Exception as exc:
            log.error("Cycle error: %s", exc)
        time.sleep(POLL_INTERVAL_SECONDS)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--once", action="store_true", help="Run a single sync pass and exit")
    args = parser.parse_args()

    if args.once:
        results = run_once()
        print("\n=== Risultati Sync ===")
        print(f"{'Stato':<12} {'Email':<40} {'HubSpot ID'}")
        print("-" * 72)
        for r in results:
            print(f"{r.status.value:<12} {r.email:<40} {r.hubspot_id or '—'}")
        print(f"\nTotale: {len(results)} email processate")
    else:
        run_continuous()
