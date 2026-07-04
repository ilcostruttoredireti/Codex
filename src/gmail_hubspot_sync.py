#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail inbox and syncs senders as HubSpot contacts.

Usage:
    python src/gmail_hubspot_sync.py

Environment variables:
    HUBSPOT_ACCESS_TOKEN   HubSpot private app token (required)
    GOOGLE_CREDENTIALS     Path to Google OAuth credentials.json (default: credentials.json)
    GOOGLE_TOKEN           Path to Google OAuth token.json (default: token.json)
    SYNC_HOURS             How many hours back to scan (default: 24)
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"
STATE_FILE = Path(".gmail_sync_state.json")

# Patterns that identify automated/no-reply senders — skip these
_SKIP_RE = re.compile(
    r"noreply|no-reply|donotreply|do-not-reply|"
    r"notifications?@|notify-|automailer|mailer-daemon|"
    r"@(accounts|sc-noreply|googlebase-noreply|notify-noreply)\.",
    re.IGNORECASE,
)

# Well-known automated domains to skip entirely
_SKIP_DOMAINS = {
    "google.com", "googleapis.com", "youtube.com",
    "accounts.google.com", "mail.google.com",
}


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_thread_ids": [], "last_run": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _get_gmail_service(credentials_path: str, token_path: str):
    creds: Optional[Credentials] = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _should_skip(email: str) -> bool:
    if _SKIP_RE.search(email):
        return True
    domain = email.split("@")[-1].lower()
    return domain in _SKIP_DOMAINS


def _parse_from_header(from_header: str) -> tuple[str, str]:
    """Return (display_name, email) from a From: header value."""
    m = re.match(r'^(.+?)\s*<(.+?)>\s*$', from_header)
    if m:
        return m.group(1).strip().strip('"\''), m.group(2).strip().lower()
    return "", from_header.strip().lower()


def fetch_inbox_threads(service, since_hours: int = 24) -> list[dict]:
    """Return list of thread dicts with sender info for recent inbox messages."""
    after_ts = int((datetime.utcnow() - timedelta(hours=since_hours)).timestamp())
    query = f"in:inbox -from:me after:{after_ts}"

    result = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    messages = result.get("messages", [])

    threads: list[dict] = []
    for msg in messages:
        detail = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        display_name, sender_email = _parse_from_header(headers.get("From", ""))

        if not sender_email or _should_skip(sender_email):
            continue

        threads.append({
            "thread_id": detail.get("threadId", msg["id"]),
            "message_id": msg["id"],
            "sender_email": sender_email,
            "display_name": display_name,
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
        })

    return threads


# ---------------------------------------------------------------------------
# Name/company inference helpers
# ---------------------------------------------------------------------------

def _infer_name(email_local: str) -> tuple[str, str]:
    """Best-effort firstname/lastname from the local part of an email."""
    parts = re.split(r"[._\-+]", email_local)
    if len(parts) >= 2:
        return parts[0].capitalize(), parts[1].capitalize()
    return email_local.capitalize(), ""


def _company_from_domain(domain: str) -> str:
    name = domain.split(".")[0]
    name = re.sub(r"([A-Z])", r" \1", name).strip()
    name = name.replace("-", " ").replace("_", " ")
    return name.title()


def _extract_contact_info(thread: dict) -> dict:
    email = thread["sender_email"]
    display_name = thread["display_name"]
    domain = email.split("@")[-1]

    if display_name:
        parts = display_name.split(None, 1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""
    else:
        firstname, lastname = _infer_name(email.split("@")[0])

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": _company_from_domain(domain),
        "domain": domain,
    }


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _hs_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def find_contact(email: str, token: str) -> Optional[dict]:
    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(token),
        json={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        },
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(props: dict, token: str) -> dict:
    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(token),
        json={"properties": props},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def update_contact(contact_id: str, props: dict, token: str) -> dict:
    resp = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(token),
        json={"properties": props},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def add_note(contact_id: str, subject: str, sender_email: str, token: str) -> None:
    """Log a timeline note for the received email."""
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    body = (
        f"Email ricevuta da Gmail\n"
        f"Mittente: {sender_email}\n"
        f"Oggetto: {subject}"
    )
    resp = requests.post(
        f"{HUBSPOT_BASE}/engagements/v1/engagements",
        headers=_hs_headers(token),
        json={
            "engagement": {"active": True, "type": "NOTE", "timestamp": now_ms},
            "associations": {"contactIds": [int(contact_id)]},
            "metadata": {"body": body},
        },
        timeout=10,
    )
    if not resp.ok:
        logger.debug("Note creation returned %s: %s", resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

# Placeholder values that indicate the name was auto-filled from the email prefix
_GENERIC_NAMES = {"info", "team", "product", "commerciale", "redazione", "contact",
                  "hello", "hi", "mail", "admin", "support", "sales", "news"}


def sync_sender(thread: dict, token: str) -> dict:
    """Ensure a Gmail sender exists (and is up-to-date) in HubSpot."""
    info = _extract_contact_info(thread)
    email = info["email"]

    existing = find_contact(email, token)

    if existing:
        contact_id = existing["id"]
        current = existing.get("properties", {})

        updates: dict = {}

        # Improve generic firstname/lastname
        if not current.get("firstname") or current["firstname"].lower() in _GENERIC_NAMES:
            if info["firstname"]:
                updates["firstname"] = info["firstname"]
        if not current.get("lastname") and info["lastname"]:
            updates["lastname"] = info["lastname"]
        if not current.get("company") and info["company"]:
            updates["company"] = info["company"]

        # Mark lead source if unset
        if not current.get("hs_lead_source"):
            updates["hs_lead_source"] = "OFFLINE"  # closest standard enum value

        if updates:
            update_contact(contact_id, updates, token)
            status = "Aggiornato"
        else:
            status = "Ignorato"
    else:
        new_props = {
            "email": email,
            "firstname": info["firstname"],
            "lastname": info["lastname"],
            "company": info["company"],
            "hs_lead_source": "OFFLINE",
        }
        created = create_contact(new_props, token)
        contact_id = created["id"]
        status = "Creato"

    # Optional: add timeline note for the received email
    try:
        add_note(contact_id, thread["subject"], email, token)
    except Exception as exc:
        logger.debug("Note skipped for %s: %s", email, exc)

    return {"status": status, "email": email, "hubspot_id": contact_id}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> list[dict]:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        logger.error("HUBSPOT_ACCESS_TOKEN is not set")
        sys.exit(1)

    credentials_path = os.environ.get("GOOGLE_CREDENTIALS", "credentials.json")
    token_path = os.environ.get("GOOGLE_TOKEN", "token.json")
    since_hours = int(os.environ.get("SYNC_HOURS", "24"))

    state = load_state()
    processed_ids: set = set(state.get("processed_thread_ids", []))

    logger.info("Connecting to Gmail…")
    service = _get_gmail_service(credentials_path, token_path)

    logger.info("Fetching inbox threads from the last %d hours…", since_hours)
    threads = fetch_inbox_threads(service, since_hours=since_hours)
    logger.info("Found %d non-automated sender(s) before deduplication", len(threads))

    # Deduplicate: one contact per unique sender email, skip already-processed threads
    seen_emails: set = set()
    unique_threads: list[dict] = []
    for t in threads:
        if t["sender_email"] not in seen_emails and t["thread_id"] not in processed_ids:
            seen_emails.add(t["sender_email"])
            unique_threads.append(t)

    logger.info("Processing %d unique new sender(s)…", len(unique_threads))

    results: list[dict] = []
    for thread in unique_threads:
        try:
            result = sync_sender(thread, token)
            results.append(result)
            processed_ids.add(thread["thread_id"])
            logger.info("[%s] %s → HubSpot ID %s", result["status"], result["email"], result["hubspot_id"])
        except requests.HTTPError as exc:
            logger.error("HTTP error for %s: %s", thread["sender_email"], exc)
            results.append({"status": "Errore", "email": thread["sender_email"], "hubspot_id": None})
        except Exception as exc:
            logger.error("Unexpected error for %s: %s", thread["sender_email"], exc)
            results.append({"status": "Errore", "email": thread["sender_email"], "hubspot_id": None})

    # Persist state (cap at 5000 entries to avoid unbounded growth)
    state["processed_thread_ids"] = list(processed_ids)[-5000:]
    state["last_run"] = datetime.utcnow().isoformat()
    save_state(state)

    # -------------------------------------------------------------------------
    # Summary table
    # -------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("  Riepilogo Sincronizzazione Gmail → HubSpot")
    print("=" * 70)
    print(f"{'Stato':<14} {'Email':<42} ID HubSpot")
    print("-" * 70)
    for r in results:
        print(f"{r['status']:<14} {r['email']:<42} {r.get('hubspot_id') or 'N/A'}")

    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    ignored = sum(1 for r in results if r["status"] == "Ignorato")
    errors  = sum(1 for r in results if r["status"] == "Errore")
    print("-" * 70)
    print(
        f"Totale: {len(results)}  |  Creati: {created}  |  Aggiornati: {updated}"
        f"  |  Ignorati: {ignored}  |  Errori: {errors}"
    )
    print("=" * 70)

    return results


if __name__ == "__main__":
    main()
