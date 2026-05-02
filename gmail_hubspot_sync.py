#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Polls the Gmail inbox for new inbound emails and upserts the sender
as a HubSpot contact.  Run once (--once) or continuously (default).

Environment variables (see .env.example):
  HUBSPOT_TOKEN           – HubSpot Private App token  (required)
  GMAIL_CREDENTIALS_FILE  – path to Google OAuth credentials JSON (default: credentials.json)
  GMAIL_TOKEN_FILE        – path to cached OAuth token pickle     (default: token.pickle)
  STATE_FILE              – JSON file for incremental-sync state  (default: sync_state.json)
  POLL_INTERVAL_SECONDS   – seconds between polls in daemon mode  (default: 60)
  LOG_LEVEL               – DEBUG / INFO / WARNING / ERROR        (default: INFO)
"""

import argparse
import json
import logging
import os
import pickle
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Bootstrap ─────────────────────────────────────────────────────────────────

load_dotenv()

HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.pickle")
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"

# Common personal/free email domains – no company name derived from these
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "ymail.com",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "aol.com", "mail.com", "gmx.com", "gmx.net", "zoho.com",
    "tutanota.com", "fastmail.com", "libero.it", "alice.it", "virgilio.it",
    "tiscali.it", "tin.it", "email.it",
}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hs_sync")

# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    token_path = Path(GMAIL_TOKEN_FILE)

    if token_path.exists():
        with open(token_path, "rb") as fh:
            creds = pickle.load(fh)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(token_path, "wb") as fh:
            pickle.dump(creds, fh)

    return build("gmail", "v1", credentials=creds)


def get_new_messages(service, state: dict) -> tuple[list[dict], str]:
    """Return (message_stubs, new_history_id) for INBOX messages added since
    the last recorded historyId.  Falls back to a small initial batch on first run."""
    history_id = state.get("gmail_history_id")

    if history_id:
        try:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            messages: list[dict] = []
            for record in resp.get("history", []):
                for item in record.get("messagesAdded", []):
                    messages.append(item["message"])
            return messages, resp.get("historyId", history_id)
        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId is too old – fall through to full resync
                log.warning("historyId expired; falling back to recent-messages scan")
            else:
                raise

    # First run or expired historyId – grab last 25 INBOX messages as baseline
    resp = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=25)
        .execute()
    )
    messages = resp.get("messages", [])
    profile = service.users().getProfile(userId="me").execute()
    return messages, profile["historyId"]


def fetch_message_headers(service, msg_id: str) -> Optional[dict]:
    try:
        return (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
    except HttpError as exc:
        log.warning("Could not fetch message %s: %s", msg_id, exc)
        return None


def extract_sender(message: dict) -> Optional[tuple[str, str, str]]:
    """Parse From header; returns (email, display_name, domain) or None."""
    headers = {
        h["name"]: h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }
    raw_from = headers.get("From", "")
    display_name, email_addr = parseaddr(raw_from)
    if not email_addr or "@" not in email_addr:
        return None
    email_addr = email_addr.lower().strip()
    display_name = display_name.strip().strip('"')
    domain = email_addr.split("@", 1)[1].lower()
    return email_addr, display_name, domain


# ── Contact data helpers ──────────────────────────────────────────────────────

def company_from_domain(domain: str) -> Optional[str]:
    """Derive a human-readable company name from a business email domain."""
    if domain in PERSONAL_DOMAINS:
        return None
    # Strip up to two TLDs: "acme-corp.co.uk" → "acme-corp"
    base = re.sub(r"\.[^.]+$", "", domain)
    base = re.sub(r"\.[^.]+$", "", base)
    if not base:
        return None
    return base.replace("-", " ").replace("_", " ").replace(".", " ").title()


def split_display_name(full_name: str) -> tuple[str, str]:
    """Return (firstname, lastname); lastname may be empty."""
    parts = full_name.strip().split(None, 1)  # split on first whitespace only
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def build_contact_properties(email: str, name: str, domain: str) -> dict:
    first, last = split_display_name(name)
    company = company_from_domain(domain)
    props: dict[str, str] = {
        "email": email,
        "hs_analytics_source": "EMAIL_MARKETING",
        "hs_analytics_source_data_1": "Inbound Gmail",
    }
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company
    return props


# ── HubSpot API calls ─────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email: str) -> Optional[dict]:
    """Return the HubSpot contact record for *email*, or None."""
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": [
            "email", "firstname", "lastname", "company",
            "hs_analytics_source", "hs_analytics_source_data_1",
        ],
        "limit": 1,
    }
    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> dict:
    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def hs_patch_contact(contact_id: str, current_props: dict, new_props: dict) -> bool:
    """Patch only fields that are currently empty in HubSpot.
    Returns True if at least one field was updated."""
    updates = {
        k: v
        for k, v in new_props.items()
        if k != "email" and v and not current_props.get(k)
    }
    if not updates:
        return False
    r = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
        timeout=15,
    )
    r.raise_for_status()
    return True


def hs_add_email_note(contact_id: str, sender_email: str, snippet: str) -> None:
    """Attach an activity note to the contact recording the inbound email."""
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body_text = f"📥 Inbound Gmail – {sender_email}"
    if snippet:
        body_text += f"\n\n{snippet[:300]}"

    payload = {
        "properties": {
            "hs_note_body": body_text,
            "hs_timestamp": str(timestamp_ms),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # Note → Contact
                    }
                ],
            }
        ],
    }
    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/notes",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()


# ── Core upsert logic ─────────────────────────────────────────────────────────

def upsert_contact(
    email: str, name: str, domain: str, snippet: str = ""
) -> tuple[str, str]:
    """Upsert sender as HubSpot contact.

    Returns (status, contact_id) where status is one of:
      'created'  – new contact was created
      'updated'  – existing contact had missing fields filled in
      'ignored'  – existing contact already had all relevant data
    """
    new_props = build_contact_properties(email, name, domain)
    existing = hs_find_contact(email)

    if existing:
        contact_id = existing["id"]
        was_updated = hs_patch_contact(
            contact_id, existing.get("properties", {}), new_props
        )
        status = "updated" if was_updated else "ignored"
    else:
        created = hs_create_contact(new_props)
        contact_id = created["id"]
        status = "created"

    try:
        hs_add_email_note(contact_id, email, snippet)
    except Exception as exc:
        log.warning("Could not add note for contact %s: %s", contact_id, exc)

    return status, contact_id


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_once(service, state: dict) -> dict:
    """Execute one sync cycle; mutates and returns updated *state*."""
    messages, new_history_id = get_new_messages(service, state)
    processed: set[str] = set(state.get("processed_ids", []))
    report: list[dict] = []

    log.info("Poll complete – %d candidate message(s) to evaluate", len(messages))

    for stub in messages:
        msg_id = stub["id"]
        if msg_id in processed:
            continue

        message = fetch_message_headers(service, msg_id)
        if not message:
            processed.add(msg_id)
            continue

        sender = extract_sender(message)
        if not sender:
            log.debug("Message %s: no parseable From header – skipped", msg_id)
            processed.add(msg_id)
            continue

        email, name, domain = sender
        snippet = message.get("snippet", "")

        try:
            status, contact_id = upsert_contact(email, name, domain, snippet)
            entry = {
                "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "status": status,
                "email": email,
                "hubspot_id": contact_id,
            }
            report.append(entry)
            log.info("%-8s │ %-45s │ HS-ID: %s", status.upper(), email, contact_id)
        except requests.HTTPError as exc:
            log.error("HubSpot error for %s: %s – %s", email, exc, exc.response.text)
        except Exception as exc:
            log.error("Unexpected error for %s: %s", email, exc)

        processed.add(msg_id)

    # Print tabular report for this cycle
    if report:
        separator = "─" * 75
        print(f"\n{separator}")
        print(f"  Sync Report  [{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC]")
        print(separator)
        print(f"  {'STATUS':<10} {'EMAIL':<45} {'HUBSPOT ID'}")
        print(separator)
        for row in report:
            print(f"  {row['status'].upper():<10} {row['email']:<45} {row['hubspot_id']}")
        print(f"{separator}\n")

    # Persist state – cap processed_ids to avoid unbounded growth
    state["gmail_history_id"] = new_history_id
    state["processed_ids"] = list(processed)[-10_000:]
    return state


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Gmail inbound senders to HubSpot contacts"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync cycle then exit (default: continuous daemon)",
    )
    args = parser.parse_args()

    if not HUBSPOT_TOKEN:
        raise SystemExit("HUBSPOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if not Path(GMAIL_CREDENTIALS_FILE).exists():
        raise SystemExit(
            f"Gmail credentials file not found: {GMAIL_CREDENTIALS_FILE}\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )

    log.info("Starting Gmail → HubSpot sync  (mode: %s)", "once" if args.once else "daemon")
    service = get_gmail_service()
    state = load_state()

    # First run: record baseline historyId without processing backlog
    if "gmail_history_id" not in state:
        log.info("First run – recording baseline historyId (no backlog processed)")
        profile = service.users().getProfile(userId="me").execute()
        state["gmail_history_id"] = profile["historyId"]
        state["processed_ids"] = []
        save_state(state)
        log.info("Baseline saved. New emails arriving from now will be synced.")
        if args.once:
            return

    if args.once:
        state = run_once(service, state)
        save_state(state)
        return

    # Daemon mode
    log.info("Polling every %d seconds. Press Ctrl+C to stop.", POLL_INTERVAL)
    while True:
        try:
            state = run_once(service, state)
            save_state(state)
        except KeyboardInterrupt:
            log.info("Interrupted – exiting.")
            break
        except Exception as exc:
            log.error("Unhandled error in sync cycle: %s", exc, exc_info=True)
        log.debug("Sleeping %ds …", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
