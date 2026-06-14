#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and syncs senders as contacts in HubSpot.
Uses email as unique key; creates new contacts or updates missing fields.
"""

import os
import re
import json
import base64
import logging
import time
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json")
GMAIL_CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "gmail_credentials.json")
GMAIL_QUERY = os.environ.get("GMAIL_QUERY", "in:inbox -from:me")
GMAIL_MAX_RESULTS = int(os.environ.get("GMAIL_MAX_RESULTS", "100"))

HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]
HUBSPOT_BASE = "https://api.hubapi.com"

# Number of days to look back on the first run (or when no state file exists)
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "1"))
STATE_FILE = os.environ.get("STATE_FILE", ".sync_state.json")

# Email addresses to skip (your own addresses, bots, bouncers)
SKIP_SENDERS: set[str] = {
    addr.lower().strip()
    for addr in os.environ.get(
        "SKIP_SENDERS",
        "mailer-daemon@googlemail.com,noreply@google.com",
    ).split(",")
    if addr.strip()
}

# Domain patterns whose emails are skip-worthy (notification services, etc.)
SKIP_DOMAINS: set[str] = {
    d.lower().strip()
    for d in os.environ.get(
        "SKIP_DOMAINS",
        "facebookmail.com,googlemail.com",
    ).split(",")
    if d.strip()
}

# ── state ─────────────────────────────────────────────────────────────────────


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Gmail helpers ─────────────────────────────────────────────────────────────


def get_gmail_service():
    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_messages(service, after_epoch: int | None = None) -> list[dict]:
    """Return all messages matching GMAIL_QUERY since after_epoch."""
    query = GMAIL_QUERY
    if after_epoch:
        query += f" after:{after_epoch}"

    messages: list[dict] = []
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": GMAIL_MAX_RESULTS}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_sender(service, msg_id: str) -> tuple[str, str]:
    """Return (name, email) for the sender of a message."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="metadata", metadataHeaders=["From", "Subject"])
        .execute()
    )
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    raw_from = headers.get("from", "")
    name, email = parseaddr(raw_from)
    return name.strip(), email.strip().lower()


# ── HubSpot helpers ───────────────────────────────────────────────────────────


def hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def search_contact(email: str) -> dict | None:
    """Return existing HubSpot contact dict or None."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    r = httpx.post(url, headers=hs_headers(), json=payload, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def _company_from_domain(email: str) -> str:
    """Derive a best-effort company name from the email domain."""
    personal_domains = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "icloud.com", "libero.it", "tiscali.it", "alice.it",
        "virgilio.it", "tin.it", "live.com",
    }
    domain = email.split("@")[-1].lower()
    if domain in personal_domains:
        return ""
    # Turn "mycompany.co.uk" → "mycompany"
    parts = domain.split(".")
    return parts[0].replace("-", " ").title()


def _split_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (firstname, lastname)."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return display_name.strip(), ""


def create_contact(name: str, email: str) -> str:
    """Create a new HubSpot contact; return its ID."""
    firstname, lastname = _split_name(name) if name else ("", "")
    company = _company_from_domain(email)
    properties: dict = {
        "email": email,
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if firstname:
        properties["firstname"] = firstname
    if lastname:
        properties["lastname"] = lastname
    if company:
        properties["company"] = company

    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    r = httpx.post(url, headers=hs_headers(), json={"properties": properties}, timeout=15)
    r.raise_for_status()
    contact_id = r.json()["id"]
    _add_note(contact_id, f"Contatto acquisito da Gmail Inbound. Email: {email}")
    return contact_id


def update_contact(contact_id: str, name: str, email: str, existing: dict) -> None:
    """Patch only fields that are missing."""
    props = existing.get("properties", {})
    updates: dict = {}

    if not props.get("company"):
        company = _company_from_domain(email)
        if company:
            updates["company"] = company

    if name and not props.get("firstname"):
        firstname, lastname = _split_name(name)
        updates["firstname"] = firstname
        if lastname and not props.get("lastname"):
            updates["lastname"] = lastname

    if not props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"

    if updates:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
        r = httpx.patch(url, headers=hs_headers(), json={"properties": updates}, timeout=15)
        r.raise_for_status()
        _add_note(contact_id, "Contatto aggiornato da Gmail Inbound: " + ", ".join(updates.keys()))


def _add_note(contact_id: str, body: str) -> None:
    """Add an engagement note associated with a contact."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    note_payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": str(now_ms),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}
                ],
            }
        ],
    }
    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    r = httpx.post(url, headers=hs_headers(), json=note_payload, timeout=15)
    # Notes are best-effort; log but don't raise
    if r.status_code >= 400:
        log.warning("Note creation failed %s: %s", r.status_code, r.text[:200])


# ── main sync loop ─────────────────────────────────────────────────────────────


def sync_once(service) -> list[dict]:
    """Run a single sync pass; return list of result dicts."""
    state = load_state()
    last_epoch = state.get("last_epoch")

    if not last_epoch:
        cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        last_epoch = int(cutoff.timestamp())

    messages = list_messages(service, after_epoch=last_epoch)
    log.info("Found %d messages since epoch %d", len(messages), last_epoch)

    processed_ids: set[str] = set(state.get("processed_ids", []))
    results: list[dict] = []
    new_epoch = last_epoch

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed_ids:
            continue

        try:
            name, email = get_sender(service, msg_id)
        except Exception as exc:
            log.warning("Could not fetch sender for %s: %s", msg_id, exc)
            processed_ids.add(msg_id)
            continue

        if not email or "@" not in email:
            processed_ids.add(msg_id)
            continue

        domain = email.split("@")[-1].lower()
        if email in SKIP_SENDERS or domain in SKIP_DOMAINS:
            log.debug("Skipping %s", email)
            processed_ids.add(msg_id)
            continue

        try:
            existing = search_contact(email)
            if existing:
                contact_id = existing["id"]
                update_contact(contact_id, name, email, existing)
                status = "Aggiornato"
            else:
                contact_id = create_contact(name, email)
                status = "Creato"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                # Duplicate — already exists under a different search path
                status = "Ignorato (duplicato)"
                contact_id = "—"
            else:
                log.error("HubSpot error for %s: %s", email, exc)
                status = "Errore"
                contact_id = "—"

        result = {"stato": status, "email": email, "hubspot_id": contact_id}
        results.append(result)
        log.info("%s | %s | ID=%s", status, email, contact_id)
        processed_ids.add(msg_id)

        # Rate-limit: HubSpot allows ~10 req/s on the free tier
        time.sleep(0.12)

    # Advance the epoch to now so the next run only looks at truly new mail
    new_epoch = int(datetime.now(timezone.utc).timestamp())
    save_state(
        {
            "last_epoch": new_epoch,
            "processed_ids": list(processed_ids)[-2000:],  # cap list size
        }
    )
    return results


def run(poll_interval_seconds: int = 0) -> None:
    """
    Run sync.
    If poll_interval_seconds > 0, loop continuously.
    If 0 (default), run once and exit (suitable for cron / scheduled triggers).
    """
    service = get_gmail_service()
    while True:
        log.info("── Sync avviato %s ──", datetime.now(timezone.utc).isoformat())
        results = sync_once(service)
        _print_summary(results)
        if poll_interval_seconds <= 0:
            break
        log.info("Attendo %ds prima del prossimo controllo…", poll_interval_seconds)
        time.sleep(poll_interval_seconds)


def _print_summary(results: list[dict]) -> None:
    if not results:
        log.info("Nessun nuovo contatto da processare.")
        return
    print("\n── Risultati sincronizzazione ──")
    print(f"{'Stato':<25} {'Email':<45} {'ID HubSpot'}")
    print("-" * 90)
    for r in results:
        print(f"{r['stato']:<25} {r['email']:<45} {r['hubspot_id']}")
    created = sum(1 for r in results if r["stato"] == "Creato")
    updated = sum(1 for r in results if r["stato"] == "Aggiornato")
    skipped = len(results) - created - updated
    print(f"\nTotale: {len(results)} | Creati: {created} | Aggiornati: {updated} | Ignorati: {skipped}\n")


if __name__ == "__main__":
    import sys

    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    run(poll_interval_seconds=interval)
