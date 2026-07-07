#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox, extracts sender info, and syncs contacts to HubSpot.
"""

import json
import os
import re
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ─── Configuration ────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min

CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Sender prefixes / domains to skip (automated/no-reply addresses)
SKIP_PREFIXES = {
    "no-reply", "noreply", "dont-reply", "do-not-reply", "donotreply",
    "nobody", "notifications-noreply", "ads-noreply", "notify-noreply",
    "sc-noreply", "system", "mailer-daemon", "postmaster", "bounce",
    "automated", "automailer",
}
SKIP_DOMAINS = {
    "linkedin.com", "discord.com", "google.com", "facebook.com",
    "twitter.com", "instagram.com", "tiktok.com", "freemius.com",
    "feedspot.com",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Gmail helpers ────────────────────────────────────────────────────────────

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
        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_new_messages(service, since_timestamp: Optional[int] = None) -> list[dict]:
    """Return list of message dicts with 'from', 'subject', 'date' keys."""
    query = "in:inbox -from:me"
    if since_timestamp:
        query += f" after:{since_timestamp}"
    results = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                pageToken=page_token,
                maxResults=500,
            )
            .execute()
        )
        for msg_ref in resp.get("messages", []):
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_ref["id"], format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            results.append({
                "id": msg_ref["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


# ─── Contact parsing ──────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> Optional[dict]:
    """
    Parse 'Name <email>' or bare email into a contact dict.
    Returns None if the sender should be skipped.
    """
    match = re.match(r"^(.+?)\s*<(.+?)>$", from_header.strip())
    if match:
        display_name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        display_name = ""
        email = from_header.strip().lower()

    # Validate email format
    if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        return None

    local, domain = email.split("@", 1)

    # Skip automated / no-reply senders
    if local in SKIP_PREFIXES or any(local.startswith(p + "@") for p in SKIP_PREFIXES):
        return None
    if domain in SKIP_DOMAINS:
        return None
    for prefix in SKIP_PREFIXES:
        if local.startswith(prefix):
            return None

    # Try to split display name into first / last
    name_parts = display_name.split() if display_name else []
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    # Fall back to parsing from the local part (e.g. "john.doe" → John / Doe)
    if not first_name and "." in local:
        parts = local.split(".")
        first_name = parts[0].capitalize()
        last_name = parts[1].capitalize() if len(parts) > 1 else ""

    # Derive company from domain (strip TLD, capitalise)
    root = domain.split(".")[0]
    company = root.replace("-", " ").title()

    return {
        "email": email,
        "firstname": first_name,
        "lastname": last_name,
        "company": company,
        "domain": domain,
    }


# ─── HubSpot helpers ──────────────────────────────────────────────────────────

HUBSPOT_BASE = "https://api.hubapi.com"
HEADERS = lambda: {
    "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


def hs_search_contact(email: str) -> Optional[dict]:
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status",
                        "lead_source"],
        "limit": 1,
    }
    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=HEADERS(),
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("total", 0) > 0:
        return data["results"][0]
    return None


def hs_create_contact(contact: dict) -> dict:
    properties = {
        "email": contact["email"],
        "lead_source": CONTACT_SOURCE,
        "hs_lead_status": "NEW",
    }
    if contact.get("firstname"):
        properties["firstname"] = contact["firstname"]
    if contact.get("lastname"):
        properties["lastname"] = contact["lastname"]
    if contact.get("company"):
        properties["company"] = contact["company"]

    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=HEADERS(),
        json={"properties": properties},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, updates: dict) -> dict:
    resp = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=HEADERS(),
        json={"properties": updates},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def hs_add_note(contact_id: str, subject: str, email_date: str) -> None:
    """Attach a Gmail activity note to the contact timeline."""
    note_body = f"Email ricevuta via Gmail\nOggetto: {subject}\nData: {email_date}"
    engagement = {
        "engagement": {"active": True, "type": "NOTE"},
        "associations": {"contactIds": [int(contact_id)]},
        "metadata": {"body": note_body},
    }
    resp = requests.post(
        f"{HUBSPOT_BASE}/engagements/v1/engagements",
        headers=HEADERS(),
        json=engagement,
        timeout=10,
    )
    if not resp.ok:
        log.warning("Note creation failed (%s): %s", resp.status_code, resp.text[:200])


# ─── State management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_check_timestamp": None, "processed_message_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── Core sync logic ──────────────────────────────────────────────────────────

def process_messages(messages: list[dict], processed_ids: set) -> list[dict]:
    """
    Deduplicate senders, skip already-processed messages, sync to HubSpot.
    Returns a list of result dicts for logging.
    """
    seen_emails: dict[str, dict] = {}  # email → best message metadata
    for msg in messages:
        if msg["id"] in processed_ids:
            continue
        contact = parse_sender(msg["from"])
        if contact is None:
            continue
        email = contact["email"]
        # Keep one representative message per unique sender
        if email not in seen_emails:
            seen_emails[email] = {"contact": contact, "msg": msg}

    results = []
    for email, data in seen_emails.items():
        contact = data["contact"]
        msg = data["msg"]
        try:
            existing = hs_search_contact(email)
            if existing:
                contact_id = existing["id"]
                props = existing["properties"]
                updates = {}
                if not props.get("company") and contact.get("company"):
                    updates["company"] = contact["company"]
                if not props.get("firstname") and contact.get("firstname"):
                    updates["firstname"] = contact["firstname"]
                if not props.get("lastname") and contact.get("lastname"):
                    updates["lastname"] = contact["lastname"]
                if updates:
                    hs_update_contact(contact_id, updates)
                    status = "Aggiornato"
                else:
                    status = "Ignorato"
            else:
                created = hs_create_contact(contact)
                contact_id = created["id"]
                hs_add_note(contact_id, msg.get("subject", ""), msg.get("date", ""))
                status = "Creato"

            results.append({
                "status": status,
                "email": email,
                "hubspot_id": contact_id,
            })
        except Exception as exc:
            log.error("Error processing %s: %s", email, exc)
            results.append({"status": "Errore", "email": email, "hubspot_id": None})

    return results


def print_results(results: list[dict]) -> None:
    if not results:
        log.info("Nessun nuovo mittente trovato.")
        return
    log.info("%-12s  %-40s  %s", "Stato", "Email", "HubSpot ID")
    log.info("-" * 75)
    for r in results:
        log.info("%-12s  %-40s  %s", r["status"], r["email"], r["hubspot_id"] or "—")


# ─── Entry point ──────────────────────────────────────────────────────────────

def run_once(service) -> None:
    state = load_state()
    since_ts = state.get("last_check_timestamp")
    processed_ids = set(state.get("processed_message_ids", []))

    log.info("Fetching Gmail inbox%s …",
             f" since {since_ts}" if since_ts else " (full scan)")
    messages = fetch_new_messages(service, since_timestamp=since_ts)
    log.info("Found %d messages", len(messages))

    results = process_messages(messages, processed_ids)
    print_results(results)

    # Update state
    new_ids = {m["id"] for m in messages}
    state["processed_message_ids"] = list(processed_ids | new_ids)
    # Keep only the last 10 000 IDs to avoid unbounded growth
    state["processed_message_ids"] = state["processed_message_ids"][-10_000:]
    state["last_check_timestamp"] = int(datetime.now(timezone.utc).timestamp())
    save_state(state)

    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    ignored = sum(1 for r in results if r["status"] == "Ignorato")
    log.info("Sync completato — Creati: %d | Aggiornati: %d | Ignorati: %d",
             created, updated, ignored)


def main():
    service = get_gmail_service()
    if POLL_INTERVAL_SECONDS == 0:
        run_once(service)
    else:
        log.info("Avvio monitoraggio continuo (intervallo: %ds) …", POLL_INTERVAL_SECONDS)
        while True:
            try:
                run_once(service)
            except Exception as exc:
                log.error("Errore nel ciclo di sync: %s", exc)
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
