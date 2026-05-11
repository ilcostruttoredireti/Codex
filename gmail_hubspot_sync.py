#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox continuously and syncs sender contacts to HubSpot.
Avoids duplicates, updates existing records, and logs inbound activity.
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

# Google API
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# HubSpot
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import (
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)
from hubspot.crm.objects.notes import (
    SimplePublicObjectInputForCreate as NoteInput,
)

# ── Config ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(".gmail_sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "")

# Domains to skip (transactional / no-reply senders)
SKIP_DOMAINS = {
    "noreply.com", "no-reply.com", "mailer.com",
    "notifications.google.com", "bounce.com",
}


# ── State management ───────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_message_ids": [], "last_history_id": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail ──────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    token_path = Path("token.json")

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_owner_email(service) -> str:
    return service.users().getProfile(userId="me").execute().get("emailAddress", "")


def fetch_new_messages(service, last_history_id: str | None, max_results: int = 100) -> list[dict]:
    """Return inbox messages added since last_history_id, or latest N if no history."""
    if last_history_id:
        try:
            history_resp = service.users().history().list(
                userId="me",
                startHistoryId=last_history_id,
                labelId="INBOX",
                historyTypes=["messageAdded"],
            ).execute()
            messages = []
            for record in history_resp.get("history", []):
                for entry in record.get("messagesAdded", []):
                    messages.append(entry["message"])
            return messages
        except HttpError as e:
            # historyId expired — fall through to full list
            log.warning(f"History expired ({e}), falling back to full inbox list.")

    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=max_results,
    ).execute()
    return result.get("messages", [])


def get_message_metadata(service, msg_id: str) -> dict:
    """Return From header + subject for a message."""
    msg = service.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From", "Subject"],
    ).execute()
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    name, email = parseaddr(headers.get("from", ""))
    return {
        "id": msg_id,
        "sender_name": name.strip(),
        "sender_email": email.strip().lower(),
        "subject": headers.get("subject", ""),
    }


def get_current_history_id(service) -> str:
    return service.users().getProfile(userId="me").execute().get("historyId", "")


# ── Contact data parsing ───────────────────────────────────────────────────────

def split_name(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def company_from_domain(domain: str) -> str:
    """Best-effort company name from email domain."""
    # Strip numeric prefixes, known personal domains return ""
    personal = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                 "icloud.com", "libero.it", "virgilio.it", "tiscali.it"}
    if domain in personal:
        return ""
    name = domain.split(".")[0]
    # Convert hyphens/underscores to spaces and title-case
    return re.sub(r"[-_]", " ", name).title()


def build_contact_fields(meta: dict) -> dict | None:
    email = meta["sender_email"]
    if not email or "@" not in email:
        return None

    domain = email.split("@")[1]
    if domain in SKIP_DOMAINS or email.startswith("noreply") or email.startswith("no-reply"):
        return None

    firstname, lastname = split_name(meta["sender_name"]) if meta["sender_name"] else ("", "")

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company_from_domain(domain),
        "hs_lead_source": "Gmail",
    }


# ── HubSpot ────────────────────────────────────────────────────────────────────

def get_hubspot_client() -> hubspot.Client:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN environment variable not set.")
    return hubspot.Client.create(access_token=token)


def find_contact_by_email(hs: hubspot.Client, email: str) -> object | None:
    filt = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[filt])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(req)
    return resp.results[0] if resp.total > 0 else None


def missing_fields(existing_contact, new_data: dict) -> dict:
    """Return only fields that are blank in the existing record."""
    current = existing_contact.properties or {}
    return {
        k: v
        for k, v in new_data.items()
        if k != "email" and v and not current.get(k)
    }


def create_contact(hs: hubspot.Client, fields: dict) -> str:
    obj = SimplePublicObjectInputForCreate(properties=fields)
    result = hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=obj
    )
    return result.id


def update_contact(hs: hubspot.Client, contact_id: str, updates: dict) -> None:
    obj = SimplePublicObjectInput(properties=updates)
    hs.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=obj,
    )


def log_inbound_email(hs: hubspot.Client, contact_id: str, subject: str, sender_email: str) -> None:
    """Attach an inbound-email note to the contact timeline."""
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    body = (
        f"📧 Email in arrivo da Gmail\n"
        f"Mittente: {sender_email}\n"
        f"Oggetto: {subject or '(nessun oggetto)'}\n"
        f"Tag: Inbound Gmail"
    )
    note = NoteInput(
        properties={"hs_note_body": body, "hs_timestamp": now_ms},
        associations=[
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # note → contact
                    }
                ],
            }
        ],
    )
    try:
        hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note
        )
    except Exception as exc:
        log.warning(f"Impossibile registrare attività email per {sender_email}: {exc}")


# ── Core processing ────────────────────────────────────────────────────────────

def process_message(
    gmail,
    hs: hubspot.Client,
    msg_id: str,
    owner_email: str,
    processed_ids: set,
) -> dict:
    """
    Process one Gmail message ID.

    Returns:
        {"status": "Creato"|"Aggiornato"|"Ignorato", "email": str, "hubspot_id": str|None}
    """
    if msg_id in processed_ids:
        return {"status": "Ignorato", "motivo": "già processato", "email": None, "hubspot_id": None}

    meta = get_message_metadata(gmail, msg_id)
    sender_email = meta["sender_email"]

    if not sender_email:
        return {"status": "Ignorato", "motivo": "mittente mancante", "email": None, "hubspot_id": None}

    if sender_email == owner_email:
        return {"status": "Ignorato", "motivo": "email proprietario account", "email": sender_email, "hubspot_id": None}

    fields = build_contact_fields(meta)
    if fields is None:
        return {"status": "Ignorato", "motivo": "dominio escluso o email non valida", "email": sender_email, "hubspot_id": None}

    existing = find_contact_by_email(hs, sender_email)

    if existing:
        updates = missing_fields(existing, fields)
        if updates:
            update_contact(hs, existing.id, updates)
            log_inbound_email(hs, existing.id, meta["subject"], sender_email)
            status = "Aggiornato"
        else:
            status = "Ignorato"  # exists and complete — no changes needed
        hubspot_id = existing.id
    else:
        hubspot_id = create_contact(hs, fields)
        log_inbound_email(hs, hubspot_id, meta["subject"], sender_email)
        status = "Creato"

    processed_ids.add(msg_id)
    return {"status": status, "email": sender_email, "hubspot_id": hubspot_id}


def print_result(result: dict) -> None:
    status = result["status"]
    email = result.get("email") or "-"
    hubspot_id = result.get("hubspot_id") or "-"
    motivo = f" ({result['motivo']})" if result.get("motivo") else ""
    print(f"  [{status:10}]  Email: {email:<45}  HubSpot ID: {hubspot_id}{motivo}")


# ── Entry point ────────────────────────────────────────────────────────────────

def run_sync():
    gmail = get_gmail_service()
    hs = get_hubspot_client()

    owner_email = OWNER_EMAIL or get_owner_email(gmail)
    log.info(f"Account Gmail: {owner_email}")

    state = load_state()
    processed_ids: set[str] = set(state.get("processed_message_ids", []))

    log.info("Sync Gmail → HubSpot avviata. Ctrl+C per interrompere.")
    print("-" * 80)

    while True:
        try:
            messages = fetch_new_messages(gmail, state.get("last_history_id"))
            new_msgs = [m for m in messages if m["id"] not in processed_ids]

            if new_msgs:
                log.info(f"{len(new_msgs)} nuovi messaggi trovati.")
                for msg in new_msgs:
                    result = process_message(gmail, hs, msg["id"], owner_email, processed_ids)
                    print_result(result)
            else:
                log.info("Nessun nuovo messaggio.")

            # Persist state
            state["last_history_id"] = get_current_history_id(gmail)
            state["processed_message_ids"] = list(processed_ids)[-1000:]
            save_state(state)

        except KeyboardInterrupt:
            log.info("Interruzione ricevuta. Salvataggio stato e uscita.")
            save_state(state)
            break
        except Exception as exc:
            log.error(f"Errore durante la sync: {exc}", exc_info=True)

        log.info(f"Prossima verifica tra {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_sync()
