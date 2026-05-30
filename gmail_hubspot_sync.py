#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors the Gmail inbox for incoming emails and upserts senders as contacts
in HubSpot, avoiding duplicates and filling in any missing fields.

Usage:
    python gmail_hubspot_sync.py           # continuous polling loop
    python gmail_hubspot_sync.py --once    # single run then exit
    python gmail_hubspot_sync.py --interval 120  # poll every 2 minutes

Required environment variables (see .env.example):
    HUBSPOT_ACCESS_TOKEN  – HubSpot Private App token
    GOOGLE_CREDENTIALS    – path to credentials.json from Google Cloud Console
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Constants ─────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
CREDENTIALS_FILE = Path(os.getenv("GOOGLE_CREDENTIALS", "credentials.json"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
HUBSPOT_API_BASE = "https://api.hubapi.com"
DEFAULT_POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Senders we never want to sync
SKIP_LOCAL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "notifications",
    "mailer-daemon", "postmaster", "bounce", "automated",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail authentication ──────────────────────────────────────────────────────

def get_gmail_service():
    """Return an authenticated Gmail API service, refreshing or requesting OAuth as needed."""
    creds: Optional[Credentials] = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Google credentials file not found: {CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Sender parsing ────────────────────────────────────────────────────────────

def _split_name(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def _company_from_domain(domain: str) -> str:
    """'mail.acme.co.uk' → 'Acme', 'acme.com' → 'Acme'."""
    cleaned = re.sub(r"^(mail|smtp|email|mx|send|info)\.", "", domain)
    parts = cleaned.split(".")
    label = parts[-2] if len(parts) >= 2 else parts[0]
    return label.capitalize()


def _is_automated(local: str) -> bool:
    return local.startswith(SKIP_LOCAL_PREFIXES)


def parse_sender(raw_from: str) -> Optional[dict]:
    """
    Parse a raw From header into contact fields.
    Returns None for automated senders (noreply, postmaster, etc.).
    """
    display_name, email = parseaddr(raw_from)
    if not email or "@" not in email:
        return None

    email = email.lower().strip()
    local, domain = email.split("@", 1)

    if _is_automated(local):
        return None

    first_name, last_name = _split_name(display_name) if display_name else ("", "")
    company = _company_from_domain(domain)

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
    }


# ── Gmail message fetching ────────────────────────────────────────────────────

def _get_message_from_header(service, msg_id: str) -> Optional[str]:
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return headers.get("From")


def fetch_new_message_ids(service, state: dict) -> tuple[list[str], str]:
    """
    Return (new_message_ids, current_history_id).
    First run bootstraps with the last 50 inbox messages.
    Subsequent runs use Gmail History API for efficiency.
    """
    profile = service.users().getProfile(userId="me").execute()
    current_history_id = profile["historyId"]
    last_history_id = state.get("history_id")

    if not last_history_id:
        log.info("First run – bootstrapping with last 50 inbox messages.")
        result = service.users().messages().list(
            userId="me", labelIds=["INBOX"], maxResults=50,
        ).execute()
        ids = [m["id"] for m in result.get("messages", [])]
        return ids, current_history_id

    try:
        history = service.users().history().list(
            userId="me",
            startHistoryId=last_history_id,
            labelId="INBOX",
            historyTypes=["messageAdded"],
        ).execute()
    except Exception as exc:
        log.warning(f"History API failed ({exc}), falling back to list.")
        result = service.users().messages().list(
            userId="me", labelIds=["INBOX"], maxResults=20,
        ).execute()
        ids = [m["id"] for m in result.get("messages", [])]
        return ids, current_history_id

    new_ids: list[str] = []
    for record in history.get("history", []):
        for added in record.get("messagesAdded", []):
            new_ids.append(added["message"]["id"])

    return new_ids, current_history_id


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN environment variable is not set.")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def find_contact_by_email(email: str) -> Optional[dict]:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "EQ", "value": email}
        ]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    resp = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(sender: dict) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts"
    payload = {
        "properties": {
            "email": sender["email"],
            "firstname": sender["first_name"],
            "lastname": sender["last_name"],
            "company": sender["company"],
            "hs_lead_source": "Gmail",
        }
    }
    resp = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def update_contact(contact_id: str, sender: dict, existing_props: dict) -> dict:
    """Patch only fields that are currently empty on the HubSpot contact."""
    updates: dict[str, str] = {}
    if not existing_props.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not existing_props.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not existing_props.get("company") and sender["company"]:
        updates["company"] = sender["company"]
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    if not updates:
        return {"id": contact_id, "_no_changes": True}

    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, json={"properties": updates}, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def add_inbound_note(contact_id: str, sender_email: str):
    """Attach a timeline note to the contact recording the inbound Gmail event."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/notes"
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    payload = {
        "properties": {
            "hs_timestamp": str(ts_ms),
            "hs_note_body": (
                f"[Inbound Gmail] Email received from {sender_email}.\n"
                f"Tag: Inbound Gmail | Fonte: Gmail"
            ),
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": 202,  # Note → Contact
            }],
        }],
    }
    try:
        resp = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        log.warning(f"Note creation failed for {sender_email}: {exc}")


# ── Sync logic ────────────────────────────────────────────────────────────────

def sync_sender(sender: dict) -> dict:
    """
    Upsert a sender into HubSpot.
    Returns: {stato, email, hubspot_id}
    """
    email = sender["email"]
    existing = find_contact_by_email(email)

    if existing:
        contact_id = existing["id"]
        result = update_contact(contact_id, sender, existing.get("properties", {}))
        stato = "Ignorato" if result.get("_no_changes") else "Aggiornato"
    else:
        created = create_contact(sender)
        contact_id = created["id"]
        stato = "Creato"

    if stato in ("Creato", "Aggiornato"):
        add_inbound_note(contact_id, email)

    return {"stato": stato, "email": email, "hubspot_id": contact_id}


# ── Main batch runner ─────────────────────────────────────────────────────────

def run_batch(service, state: dict) -> tuple[dict, list[dict]]:
    """Fetch new emails, sync senders, return updated state and results list."""
    msg_ids, new_history_id = fetch_new_message_ids(service, state)
    processed: set[str] = set(state.get("processed_ids", []))
    results: list[dict] = []

    pending = [mid for mid in msg_ids if mid not in processed]
    if pending:
        log.info(f"Processing {len(pending)} new message(s).")

    for msg_id in pending:
        try:
            raw_from = _get_message_from_header(service, msg_id)
            if not raw_from:
                processed.add(msg_id)
                continue

            sender = parse_sender(raw_from)
            if sender is None:
                log.debug(f"Skipping automated sender in message {msg_id}.")
                processed.add(msg_id)
                continue

            result = sync_sender(sender)
            results.append(result)
            processed.add(msg_id)
            log.info(
                f"  [{result['stato']:10s}] {result['email']:<40s} "
                f"→ HubSpot ID {result['hubspot_id']}"
            )
        except Exception as exc:
            log.error(f"  Error processing message {msg_id}: {exc}")

    # Cap processed set to avoid unbounded state file growth
    state.update({
        "history_id": new_history_id,
        "processed_ids": list(processed)[-5000:],
        "last_run": datetime.now(timezone.utc).isoformat(),
    })
    return state, results


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Monitor Gmail and sync senders to HubSpot contacts."
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single batch and exit (useful for cron/scheduled jobs).",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_POLL_INTERVAL,
        metavar="SECONDS",
        help=f"Seconds between polls when running continuously (default: {DEFAULT_POLL_INTERVAL}).",
    )
    args = parser.parse_args()

    service = get_gmail_service()
    state = load_state()

    log.info("Gmail → HubSpot sync started.")
    log.info(f"Mode: {'single run' if args.once else f'continuous (every {args.interval}s)'}")

    while True:
        try:
            state, results = run_batch(service, state)
            save_state(state)
            if results:
                created  = sum(1 for r in results if r["stato"] == "Creato")
                updated  = sum(1 for r in results if r["stato"] == "Aggiornato")
                ignored  = sum(1 for r in results if r["stato"] == "Ignorato")
                log.info(
                    f"Batch done – Creato: {created}, Aggiornato: {updated}, Ignorato: {ignored}"
                )
            else:
                log.info("No new senders to process.")
        except Exception as exc:
            log.error(f"Batch failed: {exc}", exc_info=True)

        if args.once:
            break

        log.info(f"Waiting {args.interval}s until next poll…")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
