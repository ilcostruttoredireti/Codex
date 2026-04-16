#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
-----------------------------
Monitors Gmail inbox for incoming emails and automatically syncs sender
contacts to HubSpot, avoiding duplicates and updating existing records.

Usage:
    python gmail_hubspot_sync.py

Environment variables:
    HUBSPOT_ACCESS_TOKEN   HubSpot private-app token  (required)
    POLL_INTERVAL_SECONDS  Seconds between cycles      (default: 300)
    GMAIL_LABEL            Gmail label to monitor      (default: INBOX)
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

# ── Optional imports (installed via requirements.txt) ──────────────────────
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    raise SystemExit("Install google-api-python-client, google-auth-oauthlib: pip install -r requirements.txt")

try:
    import hubspot
    from hubspot.crm.contacts import (
        ApiException,
        SimplePublicObjectInput,
        SimplePublicObjectInputForCreate,
    )
    from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
except ImportError:
    raise SystemExit("Install hubspot-api-client: pip install -r requirements.txt")

# ── Configuration ──────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = Path("gmail_token.json")
GMAIL_CREDENTIALS_FILE = Path("gmail_credentials.json")

HUBSPOT_ACCESS_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
GMAIL_LABEL = os.getenv("GMAIL_LABEL", "INBOX")
STATE_FILE = Path("gmail_sync_state.json")

# Senders to skip (automated / system)
IGNORED_SENDERS: set[str] = {
    "mailer-daemon@googlemail.com",
    "mailer-daemon@google.com",
    "noreply@google.com",
    "analytics-noreply@google.com",
    "no-reply@accounts.google.com",
    "posta-certificata@legalmail.it",
    "postmaster@gmail.com",
}

IGNORED_PREFIXES = (
    "mailer-daemon@",
    "noreply@",
    "no-reply@",
    "postmaster@",
    "donotreply@",
    "bounce@",
    "bounces@",
    "analytics-",
    "posta-certificata@",
    "notification",
    "auto-reply@",
    "automated@",
)

# Personal email domains → no company derived
PERSONAL_DOMAINS: set[str] = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "msn.com", "libero.it", "virgilio.it",
    "tiscali.it", "alice.it", "icloud.com", "protonmail.com", "me.com",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail ──────────────────────────────────────────────────────────────────

def get_gmail_service():
    if not GMAIL_CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Gmail credentials not found at '{GMAIL_CREDENTIALS_FILE}'. "
            "Download from Google Cloud Console → APIs & Services → Credentials."
        )
    creds = None
    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_history_id": None, "processed_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def is_automated(email: str) -> bool:
    e = email.lower()
    return e in IGNORED_SENDERS or any(e.startswith(p) for p in IGNORED_PREFIXES)


def fetch_messages(service, last_history_id: str | None) -> list[str]:
    """Return list of message IDs to process."""
    if last_history_id:
        try:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=last_history_id,
                    historyTypes=["messageAdded"],
                    labelId=GMAIL_LABEL,
                )
                .execute()
            )
            ids = []
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    ids.append(added["message"]["id"])
            return ids
        except HttpError as exc:
            if exc.resp.status == 404:
                log.warning("History ID expired — falling back to full inbox list")
            else:
                raise

    # Full inbox scan fallback
    resp = (
        service.users()
        .messages()
        .list(userId="me", labelIds=[GMAIL_LABEL], maxResults=50, q="-from:me")
        .execute()
    )
    return [m["id"] for m in resp.get("messages", [])]


def get_sender_info(service, message_id: str) -> dict | None:
    """Return sender metadata dict, or None if the message should be skipped."""
    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    raw_from = headers.get("From", "")
    display_name, email = parseaddr(raw_from)
    email = email.lower().strip()

    if not email or is_automated(email):
        return None

    return {
        "email": email,
        "name": display_name.strip(),
        "subject": headers.get("Subject", "(no subject)"),
        "date": headers.get("Date", ""),
        "history_id": msg.get("historyId"),
    }


# ── Name / company helpers ─────────────────────────────────────────────────

def parse_name(display_name: str, email: str) -> tuple[str, str]:
    """Return (firstname, lastname)."""
    if display_name:
        parts = display_name.strip().split(None, 1)
        return parts[0], (parts[1] if len(parts) > 1 else "")
    local = re.sub(r"[._\-+]", " ", email.split("@")[0]).strip()
    parts = local.split(None, 1)
    return parts[0].capitalize(), (parts[1].capitalize() if len(parts) > 1 else "")


def company_from_domain(email: str) -> str:
    domain = email.split("@")[-1].lower()
    if domain in PERSONAL_DOMAINS:
        return ""
    root = domain.rsplit(".", 1)[0]  # drop TLD
    return root.replace("-", " ").title()


# ── HubSpot ────────────────────────────────────────────────────────────────

def get_hubspot_client():
    if not HUBSPOT_ACCESS_TOKEN:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN environment variable is not set.")
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact(hs_client, email: str):
    """Return HubSpot contact object or None."""
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    resp = hs_client.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0] if resp.total > 0 else None


def contact_update_props(email: str, name: str, existing) -> dict:
    """Build dict of properties that are missing on an existing contact."""
    existing_props = existing.properties if existing else {}
    firstname, lastname = parse_name(name, email)
    company = company_from_domain(email)

    props: dict[str, str] = {}
    if firstname and not existing_props.get("firstname"):
        props["firstname"] = firstname
    if lastname and not existing_props.get("lastname"):
        props["lastname"] = lastname
    if company and not existing_props.get("company"):
        props["company"] = company
    if not existing_props.get("hs_lead_source"):
        props["hs_lead_source"] = "OTHER"  # "OTHER" is the standard HubSpot enum for unlisted sources
    return props


def create_contact_props(email: str, name: str) -> dict:
    """Build full property dict for a new contact."""
    firstname, lastname = parse_name(name, email)
    company = company_from_domain(email)
    props = {
        "email": email,
        "hs_lead_source": "OTHER",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def add_inbound_note(hs_client, contact_id: str, subject: str, date_str: str) -> None:
    """Attach an 'Inbound Gmail' note/activity to the contact."""
    timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    note_body = (
        f"[Inbound Gmail]\n"
        f"Subject: {subject}\n"
        f"Received: {date_str}"
    )
    try:
        hs_client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=(
                hubspot.crm.objects.notes.SimplePublicObjectInputForCreate(
                    properties={
                        "hs_note_body": note_body,
                        "hs_timestamp": timestamp_ms,
                    },
                    associations=[
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
                )
            )
        )
    except Exception as exc:
        log.warning(f"Could not add note to contact {contact_id}: {exc}")


def sync_contact(hs_client, sender: dict) -> dict:
    """
    Sync one sender to HubSpot.
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": str, "contact_id": str}
    """
    email = sender["email"]
    name = sender.get("name", "")

    existing = find_contact(hs_client, email)

    if existing:
        update_props = contact_update_props(email, name, existing)
        if update_props:
            hs_client.crm.contacts.basic_api.update(
                contact_id=existing.id,
                simple_public_object_input=SimplePublicObjectInput(properties=update_props),
            )
            add_inbound_note(hs_client, existing.id, sender["subject"], sender["date"])
            return {"status": "Aggiornato", "email": email, "contact_id": existing.id}
        else:
            return {"status": "Ignorato", "email": email, "contact_id": existing.id}
    else:
        new = hs_client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=create_contact_props(email, name)
            )
        )
        add_inbound_note(hs_client, new.id, sender["subject"], sender["date"])
        return {"status": "Creato", "email": email, "contact_id": new.id}


# ── Sync cycle ─────────────────────────────────────────────────────────────

def run_cycle(gmail_svc, hs_client, state: dict) -> tuple[list[dict], dict]:
    results: list[dict] = []
    seen_emails: set[str] = set()
    processed: set[str] = set(state.get("processed_ids", []))
    new_history_id: str | None = state.get("last_history_id")

    message_ids = fetch_messages(gmail_svc, state.get("last_history_id"))

    for msg_id in message_ids:
        if msg_id in processed:
            continue

        sender = get_sender_info(gmail_svc, msg_id)
        processed.add(msg_id)

        if not sender:
            continue

        email = sender["email"]
        if email in seen_emails:
            continue
        seen_emails.add(email)

        result = sync_contact(hs_client, sender)
        results.append(result)

        if sender.get("history_id") and (
            new_history_id is None or sender["history_id"] > new_history_id
        ):
            new_history_id = sender["history_id"]

        log.info(f"[{result['status']}]  {email}  →  HubSpot ID {result['contact_id']}")

    new_state = {
        "last_history_id": new_history_id,
        "processed_ids": list(processed)[-1000:],  # keep last 1000 to bound file size
    }
    return results, new_state


def print_report(results: list[dict]) -> None:
    if not results:
        print("  (nessuna nuova email da processare)\n")
        return
    col_w = 46
    print("\n" + "─" * 72)
    print(f"  {'Stato':<12} {'Email':<{col_w}} HubSpot ID")
    print("─" * 72)
    for r in results:
        print(f"  {r['status']:<12} {r['email']:<{col_w}} {r['contact_id']}")
    print("─" * 72)
    totals = {s: sum(1 for r in results if r["status"] == s) for s in ("Creato", "Aggiornato", "Ignorato")}
    print(
        f"  Totale: {len(results)}  |  "
        f"Creati: {totals['Creato']}  |  "
        f"Aggiornati: {totals['Aggiornato']}  |  "
        f"Ignorati: {totals['Ignorato']}\n"
    )


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    if not HUBSPOT_ACCESS_TOKEN:
        raise SystemExit("Set the HUBSPOT_ACCESS_TOKEN environment variable before running.")

    log.info("Gmail → HubSpot contact sync started")
    log.info(f"Poll interval: {POLL_INTERVAL}s  |  Gmail label: {GMAIL_LABEL}")

    gmail_svc = get_gmail_service()
    hs_client = get_hubspot_client()
    state = load_state()

    while True:
        log.info("── Sync cycle starting ──")
        try:
            results, state = run_cycle(gmail_svc, hs_client, state)
            save_state(state)
            print_report(results)
        except Exception as exc:
            log.error(f"Cycle failed: {exc}", exc_info=True)

        log.info(f"Sleeping {POLL_INTERVAL}s …\n")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
