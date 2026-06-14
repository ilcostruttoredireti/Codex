"""
Gmail → HubSpot Contact Sync
Monitors inbound Gmail emails and upserts senders as HubSpot contacts.

Requires environment variables:
  GMAIL_TOKEN_PATH     – path to OAuth2 token JSON for Gmail API
  HUBSPOT_ACCESS_TOKEN – private app token with contacts read/write scope
  PROCESSED_IDS_PATH   – path to a JSON file that tracks processed thread IDs
                         (auto-created on first run)
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
HUBSPOT_API_BASE = "https://api.hubapi.com"
CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

SKIP_SENDERS = {
    "mailer-daemon@googlemail.com",
    "notification@priority.facebookmail.com",
}

# Domains that belong to the mailbox owner – never import as contacts
OWNER_DOMAINS: set[str] = set()


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _gmail_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _hs_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _domain(email: str) -> str:
    """Return domain portion of an email address, lowercase."""
    return email.split("@")[-1].lower() if "@" in email else ""


def _parse_from_header(raw: str) -> tuple[str, str]:
    """
    Parse a 'From' header into (display_name, email).
    Handles: 'Name <addr>', '<addr>', 'addr'.
    """
    m = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>', raw.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    m = re.match(r'^<([^>]+)>', raw.strip())
    if m:
        return "", m.group(1).strip().lower()
    return "", raw.strip().lower()


def _split_name(display_name: str) -> tuple[str, str]:
    """Best-effort first/last name split from display name."""
    parts = display_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _derive_company(email: str, display_name: str) -> str:
    """
    Derive a company name from the email domain.
    Falls back to display_name if domain looks generic (gmail, libero …).
    """
    generic = {"gmail.com", "hotmail.com", "yahoo.com", "libero.it",
               "alice.it", "tiscali.it", "outlook.com"}
    domain = _domain(email)
    if domain and domain not in generic:
        # e.g. "nextpress.it" → "Nextpress"
        return domain.split(".")[0].capitalize()
    # Use display name as company if it looks like an org
    if display_name and not re.search(r'\s', display_name):
        return display_name  # single-word names are often org names
    return ""


# ──────────────────────────────────────────────────────────────
# Gmail
# ──────────────────────────────────────────────────────────────

def list_inbox_threads(token: str, max_results: int = 50, page_token: str | None = None) -> dict:
    params = {
        "q": "in:inbox -from:me",
        "maxResults": max_results,
    }
    if page_token:
        params["pageToken"] = page_token
    r = requests.get(
        f"{GMAIL_API_BASE}/users/me/threads",
        headers=_gmail_headers(token),
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_thread_sender(token: str, thread_id: str) -> tuple[str, str]:
    """Return (display_name, email) of the first inbound message in a thread."""
    r = requests.get(
        f"{GMAIL_API_BASE}/users/me/threads/{thread_id}",
        headers=_gmail_headers(token),
        params={"format": "metadata", "metadataHeaders": "From"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    messages = data.get("messages", [])
    for msg in messages:
        headers = {h["name"].lower(): h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("from", "")
        if from_header:
            return _parse_from_header(from_header)
    return "", ""


# ──────────────────────────────────────────────────────────────
# HubSpot
# ──────────────────────────────────────────────────────────────

def hs_find_contact(token: str, email: str) -> dict | None:
    """Search HubSpot for a contact by email. Returns the contact dict or None."""
    payload = {
        "filterGroups": [{
            "filters": [{"propertyName": "email", "operator": "EQ", "value": email}]
        }],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    r = requests.post(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(token),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(token: str, props: dict) -> dict:
    r = requests.post(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(token),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def hs_update_contact(token: str, contact_id: str, props: dict) -> dict:
    r = requests.patch(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(token),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def hs_log_activity(token: str, contact_id: str, subject: str, body: str) -> None:
    """Associate an Email activity with the contact (timeline entry)."""
    payload = {
        "properties": {
            "hs_email_direction": "INCOMING_EMAIL",
            "hs_email_subject": subject,
            "hs_email_text": body,
            "hs_timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 198}],
        }],
    }
    r = requests.post(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/emails",
        headers=_hs_headers(token),
        json=payload,
        timeout=15,
    )
    # Non-fatal: log but continue
    if not r.ok:
        print(f"  [warn] Activity log failed: {r.status_code} {r.text[:120]}")


# ──────────────────────────────────────────────────────────────
# Core sync logic
# ──────────────────────────────────────────────────────────────

def build_contact_props(email: str, display_name: str, existing: dict | None) -> dict:
    """
    Build the HubSpot property dict for create or update.
    Only includes fields that are currently blank (for updates).
    """
    first, last = _split_name(display_name)
    company = _derive_company(email, display_name)
    existing_props = (existing or {}).get("properties", {})

    props: dict[str, str] = {}

    if not existing_props.get("firstname") and first:
        props["firstname"] = first
    if not existing_props.get("lastname") and last:
        props["lastname"] = last
    if not existing_props.get("company") and company:
        props["company"] = company
    if not existing_props.get("hs_lead_source"):
        props["hs_lead_source"] = CONTACT_SOURCE

    if existing is None:
        # Always set email on create
        props["email"] = email
        # Ensure lead source is set
        props.setdefault("hs_lead_source", CONTACT_SOURCE)

    return props


def sync_sender(hs_token: str, email: str, display_name: str,
                log_activity: bool = False, subject: str = "") -> dict:
    """
    Upsert a single sender into HubSpot.
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "id": ...}
    """
    existing = hs_find_contact(hs_token, email)
    props = build_contact_props(email, display_name, existing)

    if existing is None:
        contact = hs_create_contact(hs_token, props)
        status = "Creato"
        contact_id = contact["id"]
    elif props:
        contact = hs_update_contact(hs_token, existing["id"], props)
        status = "Aggiornato"
        contact_id = existing["id"]
    else:
        status = "Ignorato"
        contact_id = existing["id"]

    if log_activity and status != "Ignorato":
        hs_log_activity(hs_token, contact_id, subject,
                        f"Inbound email from {email} received via Gmail.")

    return {"status": status, "email": email, "id": contact_id}


# ──────────────────────────────────────────────────────────────
# Persistence: track processed thread IDs
# ──────────────────────────────────────────────────────────────

def load_processed(path: str) -> set[str]:
    p = Path(path)
    if p.exists():
        return set(json.loads(p.read_text()))
    return set()


def save_processed(path: str, ids: set[str]) -> None:
    Path(path).write_text(json.dumps(sorted(ids)))


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────

def run_sync(gmail_token: str, hs_token: str,
             processed_path: str = "processed_threads.json",
             log_activities: bool = True) -> list[dict]:
    processed = load_processed(processed_path)
    results: list[dict] = []

    page_token = None
    while True:
        page = list_inbox_threads(gmail_token, max_results=50, page_token=page_token)
        threads = page.get("threads", [])

        for thread in threads:
            tid = thread["id"]
            if tid in processed:
                continue

            display_name, email = get_thread_sender(gmail_token, tid)
            email = email.lower().strip()

            # Skip invalid / system senders
            if not email or "@" not in email or email in SKIP_SENDERS:
                processed.add(tid)
                continue
            if OWNER_DOMAINS and _domain(email) in OWNER_DOMAINS:
                processed.add(tid)
                continue

            try:
                result = sync_sender(
                    hs_token, email, display_name,
                    log_activity=log_activities,
                    subject=thread.get("snippet", "")[:200],
                )
                results.append(result)
                print(f"  [{result['status']:10s}] {result['email']:<50s} → ID {result['id']}")
            except requests.HTTPError as exc:
                print(f"  [ERROR] {email}: {exc}")

            processed.add(tid)
            time.sleep(0.1)  # gentle rate-limit

        page_token = page.get("nextPageToken")
        if not page_token:
            break

    save_processed(processed_path, processed)
    return results


def main() -> None:
    gmail_token = os.environ["GMAIL_ACCESS_TOKEN"]
    hs_token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    processed_path = os.environ.get("PROCESSED_IDS_PATH", "processed_threads.json")

    print(f"\n{'='*60}")
    print(f"Gmail → HubSpot sync  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    results = run_sync(gmail_token, hs_token, processed_path)

    created = [r for r in results if r["status"] == "Creato"]
    updated = [r for r in results if r["status"] == "Aggiornato"]
    ignored = [r for r in results if r["status"] == "Ignorato"]

    print(f"\nSummary: {len(created)} creati, {len(updated)} aggiornati, {len(ignored)} ignorati")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
