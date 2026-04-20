#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail inbox messages and creates/updates HubSpot contacts.

Output per email:
  [Creato]    <email> → HubSpot ID: <id>
  [Aggiornato] <email> → HubSpot ID: <id>
  [Ignorato]  <email> → (<motivo>)
"""

import os
import json
import time
import logging
import re
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# OAuth scopes — read-only is enough to monitor inbox
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domains that belong to personal/free providers → no company inferred
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr",
    "hotmail.com", "hotmail.it", "hotmail.fr",
    "outlook.com", "outlook.it",
    "live.com", "live.it",
    "icloud.com", "me.com", "mac.com",
    "aol.com",
    "protonmail.com", "proton.me", "pm.me",
    "libero.it", "alice.it", "tiscali.it", "virgilio.it",
    "tin.it", "fastwebnet.it", "aruba.it",
    "msn.com", "ymail.com", "rocketmail.com",
}

# Automated senders to skip
SKIP_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster"}

STATE_FILE = Path("processed_messages.json")
MAX_STORED_IDS = 10_000


# ─── State persistence ────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"processed_ids": []}


def save_state(processed_ids: set):
    ids_list = list(processed_ids)[-MAX_STORED_IDS:]
    with open(STATE_FILE, "w") as f:
        json.dump({"processed_ids": ids_list}, f, indent=2)


# ─── Gmail helpers ────────────────────────────────────────────────────────────

def get_gmail_service():
    token_path = Path(os.getenv("GMAIL_TOKEN_PATH", "token.json"))
    creds_path = Path(os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json"))

    creds: Optional[Credentials] = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Google OAuth credentials not found at '{creds_path}'.\n"
                    "Download credentials.json from Google Cloud Console → "
                    "APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def fetch_new_inbox_messages(service, processed_ids: set) -> list[dict]:
    """Fetch inbox messages from the last 24h that haven't been processed yet."""
    try:
        result = service.users().messages().list(
            userId="me",
            q="in:inbox newer_than:1d",
            maxResults=50,
        ).execute()
    except HttpError as e:
        logger.error(f"Gmail list error: {e}")
        return []

    messages = result.get("messages", [])
    new_messages = []
    for msg in messages:
        if msg["id"] in processed_ids:
            continue
        try:
            detail = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            new_messages.append(detail)
        except HttpError as e:
            logger.warning(f"Could not fetch message {msg['id']}: {e}")

    return new_messages


# ─── Sender info extraction ───────────────────────────────────────────────────

def extract_sender_info(headers: list) -> Optional[dict]:
    from_header = get_header(headers, "From")
    if not from_header:
        return None

    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()
    if not email_addr or "@" not in email_addr:
        return None

    domain = email_addr.split("@")[1]

    # Parse name components
    first_name, last_name = "", ""
    if display_name:
        parts = display_name.strip().split(None, 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""
    else:
        # Best-effort: derive from local part (e.g. john.doe → John Doe)
        local_part = email_addr.split("@")[0]
        clean = re.sub(r"[._\-+]", " ", local_part).strip()
        parts = clean.split()
        first_name = parts[0].capitalize() if parts else ""
        last_name = parts[1].capitalize() if len(parts) > 1 else ""

    # Company from domain — skip personal providers
    company = ""
    if domain not in PERSONAL_DOMAINS:
        root = domain.split(".")[0]
        company = root.capitalize()

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
        "subject": get_header(headers, "Subject"),
        "date": get_header(headers, "Date"),
    }


# ─── HubSpot helpers ──────────────────────────────────────────────────────────

def get_hubspot_client():
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN is not set. "
            "Create a Private App in HubSpot and copy its access token."
        )
    return hubspot.Client.create(access_token=token)


def search_contact_by_email(client, email: str) -> Optional[dict]:
    try:
        response = client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "email",
                        "operator": "EQ",
                        "value": email,
                    }]
                }],
                "properties": ["email", "firstname", "lastname", "company", "leadsource"],
                "limit": 1,
            }
        )
        results = response.results
        if results:
            return {"id": results[0].id, "properties": results[0].properties}
        return None
    except ApiException as e:
        logger.error(f"HubSpot search error ({email}): {e}")
        return None


def _build_contact_props(sender: dict) -> dict:
    props: dict = {"email": sender["email"]}
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]
    return props


def create_contact(client, sender: dict) -> Optional[str]:
    props = _build_contact_props(sender)
    # leadsource enum — "OTHER" is the safe catch-all for non-standard sources
    props["leadsource"] = "OTHER"

    try:
        response = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create={
                "properties": props,
                "associations": [],
            }
        )
        contact_id = response.id
        add_activity_note(client, contact_id, sender)
        return contact_id
    except ApiException as e:
        logger.error(f"HubSpot create error ({sender['email']}): {e}")
        return None


def update_contact(client, contact_id: str, sender: dict, existing_props: dict) -> bool:
    updates: dict = {}
    if not existing_props.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not existing_props.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not existing_props.get("company") and sender["company"]:
        updates["company"] = sender["company"]

    if updates:
        try:
            client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input={"properties": updates},
            )
        except ApiException as e:
            logger.error(f"HubSpot update error ({contact_id}): {e}")
            return False

    # Always record the inbound email as a timeline activity
    add_activity_note(client, contact_id, sender)
    return True


def add_activity_note(client, contact_id: str, sender: dict):
    """Create a HubSpot Note associated with the contact to log the inbound email."""
    subject = sender.get("subject") or "(nessun oggetto)"
    date_str = sender.get("date") or datetime.now(timezone.utc).isoformat()
    body = (
        "📧 Email ricevuta via Gmail\n"
        f"Da: {sender['email']}\n"
        f"Oggetto: {subject}\n"
        f"Data: {date_str}\n"
        f"Tag: Inbound Gmail\n"
        "Fonte contatto: Gmail"
    )
    timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    try:
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create={
                "properties": {
                    "hs_note_body": body,
                    "hs_timestamp": timestamp_ms,
                },
                "associations": [{
                    "to": {"id": contact_id},
                    "types": [{
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # note → contact
                    }],
                }],
            }
        )
    except Exception as e:
        logger.warning(f"Could not create note for contact {contact_id}: {e}")


# ─── Per-message processing ───────────────────────────────────────────────────

def process_message(gmail_msg: dict, hubspot_client) -> dict:
    headers = gmail_msg.get("payload", {}).get("headers", [])
    msg_id = gmail_msg.get("id", "?")

    sender = extract_sender_info(headers)
    if not sender:
        return {"status": "Ignorato", "msg_id": msg_id, "motivo": "Nessun mittente valido"}

    # Skip automated senders
    local_part = sender["email"].split("@")[0].lower().replace("-", "").replace("_", "").replace(".", "")
    if any(skip in local_part for skip in SKIP_LOCAL_PARTS):
        return {"status": "Ignorato", "email": sender["email"], "motivo": "Mittente automatico"}

    existing = search_contact_by_email(hubspot_client, sender["email"])

    if existing:
        update_contact(hubspot_client, existing["id"], sender, existing.get("properties", {}))
        return {
            "status": "Aggiornato",
            "email": sender["email"],
            "contact_id": existing["id"],
        }
    else:
        contact_id = create_contact(hubspot_client, sender)
        if contact_id:
            return {
                "status": "Creato",
                "email": sender["email"],
                "contact_id": contact_id,
            }
        return {
            "status": "Ignorato",
            "email": sender["email"],
            "motivo": "Errore durante la creazione in HubSpot",
        }


def log_result(result: dict):
    status = result.get("status", "?")
    email = result.get("email", "-")
    contact_id = result.get("contact_id", "-")
    motivo = result.get("motivo", "")
    line = f"[{status}] {email} → HubSpot ID: {contact_id}"
    if motivo:
        line += f"  ({motivo})"
    logger.info(line)


# ─── Main loop ────────────────────────────────────────────────────────────────

def main():
    logger.info("=== Gmail → HubSpot Contact Sync avviato ===")

    state = load_state()
    processed_ids: set = set(state.get("processed_ids", []))

    gmail_service = get_gmail_service()
    hubspot_client = get_hubspot_client()

    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    logger.info(f"Polling Gmail ogni {poll_interval} secondi. Premi Ctrl+C per interrompere.")

    try:
        while True:
            logger.info("Controllo nuove email in arrivo...")
            new_messages = fetch_new_inbox_messages(gmail_service, processed_ids)

            if not new_messages:
                logger.info("Nessuna nuova email trovata.")
            else:
                logger.info(f"{len(new_messages)} nuove email da elaborare.")
                for msg in new_messages:
                    result = process_message(msg, hubspot_client)
                    log_result(result)
                    processed_ids.add(msg["id"])
                save_state(processed_ids)

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        logger.info("Sync interrotto dall'utente. Salvataggio stato...")
        save_state(processed_ids)
        logger.info("Stato salvato. Arrivederci.")


if __name__ == "__main__":
    main()
