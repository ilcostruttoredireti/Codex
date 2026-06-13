#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails, extracts sender info, and syncs contacts to HubSpot.
Avoids duplicates by using email as the unique key.
"""

import os
import re
import time
import base64
import logging
from datetime import datetime, timezone, timedelta
from email import message_from_bytes
from typing import Optional

import httpx
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"

HUBSPOT_API_BASE = "https://api.hubapi.com"
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

AUTOMATED_SENDERS = {
    "facebookmail.com",
    "mail.instagram.com",
    "noreply",
    "no-reply",
    "notifications",
    "bounce",
    "mailer-daemon",
    "postmaster",
}

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 300))


# ─────────────────────────── Gmail helpers ────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _is_automated(email: str) -> bool:
    email_lower = email.lower()
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    local = email_lower.split("@")[0] if "@" in email_lower else email_lower
    return (
        domain in AUTOMATED_SENDERS
        or any(kw in local for kw in ("noreply", "no-reply", "notify", "notification", "bounce"))
        or any(kw in domain for kw in AUTOMATED_SENDERS)
    )


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Return (display_name, email, domain) from a From header."""
    match = re.match(r'"?([^"<]*)"?\s*<?([^>]+)>?', from_header.strip())
    if match:
        name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        name = ""
        email = from_header.strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    return name, email, domain


def extract_forwarded_sender(body_text: str) -> Optional[tuple[str, str, str]]:
    """
    Extract the original sender from a forwarded email body.
    Looks for patterns like: Da "Name" email@domain.com
    """
    pattern = r'Da\s+"?([^"<\n]+)"?\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})'
    match = re.search(pattern, body_text, re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip().lower()
        domain = email.split("@")[-1]
        return name, email, domain
    return None


def split_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' → (first, last). Falls back to (display_name, '')."""
    parts = display_name.strip().split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return display_name, ""


def get_recent_inbox_messages(service, since_minutes: int = 60) -> list[dict]:
    """Fetch inbox messages received in the last `since_minutes` minutes."""
    after_ts = int((datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).timestamp())
    query = f"in:inbox after:{after_ts} -from:me"
    result = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    return result.get("messages", [])


def get_message_details(service, msg_id: str) -> dict:
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


def extract_body_text(payload: dict) -> str:
    """Recursively extract plain text body from a Gmail message payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore") if data else ""
    for part in payload.get("parts", []):
        text = extract_body_text(part)
        if text:
            return text
    return ""


# ─────────────────────────── HubSpot helpers ──────────────────────────────

def _hs_headers() -> dict:
    return {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}


def find_contact_by_email(email: str) -> Optional[dict]:
    """Return the HubSpot contact record if email exists, else None."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    resp = httpx.post(url, json=payload, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(props: dict) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts"
    resp = httpx.post(url, json={"properties": props}, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def update_contact(contact_id: str, props: dict) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = httpx.patch(url, json={"properties": props}, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def add_note_to_contact(contact_id: str, note_body: str, timestamp_ms: int) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/notes"
    payload = {
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": str(timestamp_ms),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    }
    resp = httpx.post(url, json=payload, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────── Core sync logic ──────────────────────────────

def build_contact_props(name: str, email: str, domain: str) -> dict:
    """Build HubSpot property dict from extracted sender data."""
    first, last = split_name(name)
    company = derive_company_from_domain(domain) if domain and domain != "gmail.com" else ""
    props: dict = {
        "email": email,
        "hs_lead_source": "OTHER",  # closest standard value; logged in note as Gmail
    }
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company
    return props


def derive_company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (strips TLD suffixes)."""
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[0].replace("-", " ").title()
    return domain.title()


def sync_sender(name: str, email: str, domain: str, subject: str, received_at: datetime) -> dict:
    """
    Ensure the sender exists as a HubSpot contact.
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "hubspot_id": ...}
    """
    if _is_automated(email):
        log.info("IGNORATO (automatico): %s", email)
        return {"status": "Ignorato", "email": email, "hubspot_id": None}

    existing = find_contact_by_email(email)
    ts_ms = int(received_at.timestamp() * 1000)
    note = (
        f"\U0001f4e7 Email ricevuta via Gmail\n"
        f"Fonte contatto: Gmail\n"
        f"Tag: Inbound Gmail\n"
        f"Data: {received_at.strftime('%d %B %Y %H:%M UTC')}\n"
        f"Oggetto: {subject}\n"
        f"Mittente: {name} <{email}>"
    )

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        updates: dict = {}
        # Only fill missing fields
        first, last = split_name(name)
        if not existing_props.get("firstname") and first:
            updates["firstname"] = first
        if not existing_props.get("lastname") and last:
            updates["lastname"] = last
        if not existing_props.get("company") and domain and domain != "gmail.com":
            updates["company"] = derive_company_from_domain(domain)

        if updates:
            update_contact(contact_id, updates)

        add_note_to_contact(contact_id, note, ts_ms)
        log.info("AGGIORNATO: %s → ID %s (campi aggiornati: %s)", email, contact_id, list(updates.keys()) or "solo nota")
        return {"status": "Aggiornato", "email": email, "hubspot_id": contact_id}
    else:
        props = build_contact_props(name, email, domain)
        new_contact = create_contact(props)
        contact_id = new_contact["id"]
        add_note_to_contact(contact_id, note, ts_ms)
        log.info("CREATO: %s → ID %s", email, contact_id)
        return {"status": "Creato", "email": email, "hubspot_id": contact_id}


def process_message(service, msg_id: str, processed_ids: set) -> list[dict]:
    """
    Process a single Gmail message. Extracts direct sender + forwarded original sender.
    Returns list of sync results.
    """
    if msg_id in processed_ids:
        return []

    msg = get_message_details(service, msg_id)
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    from_header = headers.get("From", "")
    subject = headers.get("Subject", "(senza oggetto)")
    date_str = headers.get("Date", "")

    try:
        from email.utils import parsedate_to_datetime
        received_at = parsedate_to_datetime(date_str)
    except Exception:
        received_at = datetime.now(timezone.utc)

    results = []
    seen_emails: set[str] = set()

    # 1. Direct Gmail sender
    name, email, domain = parse_sender(from_header)
    if email and email not in seen_emails:
        seen_emails.add(email)
        r = sync_sender(name, email, domain, subject, received_at)
        results.append(r)

    # 2. Forwarded original sender (parsed from body)
    body_text = extract_body_text(msg.get("payload", {}))
    fwd = extract_forwarded_sender(body_text)
    if fwd:
        fwd_name, fwd_email, fwd_domain = fwd
        if fwd_email and fwd_email not in seen_emails:
            seen_emails.add(fwd_email)
            r = sync_sender(fwd_name, fwd_email, fwd_domain, subject, received_at)
            results.append(r)

    processed_ids.add(msg_id)
    return results


# ─────────────────────────── Main loop ────────────────────────────────────

def run_once(service, processed_ids: set, since_minutes: int = 60) -> list[dict]:
    messages = get_recent_inbox_messages(service, since_minutes=since_minutes)
    log.info("Trovati %d messaggi da processare", len(messages))
    all_results = []
    for msg in messages:
        try:
            results = process_message(service, msg["id"], processed_ids)
            all_results.extend(results)
        except Exception as exc:
            log.warning("Errore msg %s: %s", msg["id"], exc)
    return all_results


def print_report(results: list[dict]) -> None:
    if not results:
        log.info("Nessun contatto da processare.")
        return
    print("\n" + "─" * 65)
    print(f"{'Stato':<12} {'Email':<42} {'HubSpot ID'}")
    print("─" * 65)
    for r in results:
        print(f"{r['status']:<12} {r['email']:<42} {r['hubspot_id'] or '—'}")
    print("─" * 65)
    totals = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0}
    for r in results:
        totals[r["status"]] = totals.get(r["status"], 0) + 1
    print(f"Creati: {totals['Creato']}  Aggiornati: {totals['Aggiornato']}  Ignorati: {totals['Ignorato']}\n")


def main():
    if not HUBSPOT_TOKEN:
        raise SystemExit("Errore: variabile HUBSPOT_ACCESS_TOKEN non impostata.")

    service = get_gmail_service()
    processed_ids: set = set()
    log.info("Avvio Gmail→HubSpot sync (polling ogni %ds)", POLL_INTERVAL_SECONDS)

    # First run: look back 24h to catch up
    results = run_once(service, processed_ids, since_minutes=1440)
    print_report(results)

    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        results = run_once(service, processed_ids, since_minutes=POLL_INTERVAL_SECONDS // 60 + 5)
        print_report(results)


if __name__ == "__main__":
    main()
