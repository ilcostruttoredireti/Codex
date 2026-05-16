"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and syncs sender contacts to HubSpot.
"""

import os
import time
import json
import logging
import re
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
GMAIL_LABEL = os.getenv("GMAIL_LABEL", "INBOX")

STATE_FILE = ".last_history_id"

IGNORED_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "outlook.com", "hotmail.com", "hotmail.it", "live.com",
    "icloud.com", "me.com", "aol.com", "protonmail.com",
    "libero.it", "virgilio.it", "tiscali.it",
}


# ── Gmail auth ────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                raise FileNotFoundError(
                    f"File '{CREDENTIALS_FILE}' non trovato. "
                    "Scarica le credenziali OAuth da Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ── HubSpot client ────────────────────────────────────────────────────────────

def get_hubspot_client():
    if not HUBSPOT_TOKEN:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN non configurato. "
            "Imposta la variabile d'ambiente nel file .env."
        )
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


# ── Sender extraction ─────────────────────────────────────────────────────────

def extract_sender(headers: list[dict]) -> dict | None:
    from_header = next(
        (h["value"] for h in headers if h["name"].lower() == "from"), None
    )
    if not from_header:
        return None

    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()
    if not email_addr or "@" not in email_addr:
        return None

    domain = email_addr.split("@")[1]

    first_name, last_name = "", ""
    if display_name:
        # Remove quoted parts like "Company Name <email>"
        clean_name = re.sub(r"<[^>]+>", "", display_name).strip().strip('"').strip()
        parts = clean_name.split()
        if len(parts) == 1:
            first_name = parts[0]
        elif len(parts) >= 2:
            first_name = parts[0]
            last_name = " ".join(parts[1:])

    company = ""
    if domain not in IGNORED_DOMAINS:
        # Best-effort company name from domain (e.g. acme.com → Acme)
        base = domain.split(".")[0]
        company = base.capitalize()

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "domain": domain,
    }


# ── HubSpot contact sync ──────────────────────────────────────────────────────

def find_contact_by_email(hs: hubspot.Client, email: str) -> dict | None:
    filter_ = Filter(property_name="email", operator="EQ", value=email)
    search_req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[filter_])],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    try:
        resp = hs.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        if resp.total > 0:
            return resp.results[0]
    except ApiException as exc:
        log.error("Errore ricerca HubSpot: %s", exc)
    return None


def build_properties(sender: dict, existing: dict | None) -> dict:
    props = {}

    if existing:
        ex_props = existing.properties
        if not ex_props.get("firstname") and sender["first_name"]:
            props["firstname"] = sender["first_name"]
        if not ex_props.get("lastname") and sender["last_name"]:
            props["lastname"] = sender["last_name"]
        if not ex_props.get("company") and sender["company"]:
            props["company"] = sender["company"]
    else:
        props["email"] = sender["email"]
        if sender["first_name"]:
            props["firstname"] = sender["first_name"]
        if sender["last_name"]:
            props["lastname"] = sender["last_name"]
        if sender["company"]:
            props["company"] = sender["company"]
        props["hs_lead_source"] = "GMAIL"

    return props


def sync_contact(hs: hubspot.Client, sender: dict) -> tuple[str, str]:
    """
    Returns (status, contact_id) where status is one of:
    'Creato', 'Aggiornato', 'Ignorato'
    """
    existing = find_contact_by_email(hs, sender["email"])
    props = build_properties(sender, existing)

    if existing:
        contact_id = existing.id
        if not props:
            return "Ignorato", contact_id
        try:
            hs.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=hubspot.crm.contacts.SimplePublicObjectInput(
                    properties=props
                ),
            )
            return "Aggiornato", contact_id
        except ApiException as exc:
            log.error("Errore aggiornamento contatto %s: %s", contact_id, exc)
            return "Ignorato", contact_id
    else:
        try:
            created = hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return "Creato", created.id
        except ApiException as exc:
            # 409 = already exists (race condition)
            if exc.status == 409:
                existing_id = _extract_id_from_409(exc)
                return "Ignorato", existing_id or "?"
            log.error("Errore creazione contatto %s: %s", sender["email"], exc)
            return "Ignorato", "?"


def _extract_id_from_409(exc: ApiException) -> str | None:
    try:
        body = json.loads(exc.body)
        return body.get("error", {}).get("id") or body.get("id")
    except Exception:
        return None


def add_activity_note(hs: hubspot.Client, contact_id: str, subject: str, received_at: str):
    """Associates a simple note to the contact timeline."""
    try:
        from hubspot.crm.objects.notes import (
            SimplePublicObjectInputForCreate as NoteCreate,
        )
        note_props = {
            "hs_note_body": f"Email ricevuta via Gmail\nOggetto: {subject}\nData: {received_at}",
            "hs_timestamp": str(int(time.time() * 1000)),
        }
        note = hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteCreate(properties=note_props)
        )
        # Associate note → contact
        hs.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as exc:
        log.debug("Nota non aggiunta (non bloccante): %s", exc)


# ── Gmail polling ─────────────────────────────────────────────────────────────

def load_last_history_id() -> str | None:
    if Path(STATE_FILE).exists():
        return Path(STATE_FILE).read_text().strip() or None
    return None


def save_history_id(history_id: str):
    Path(STATE_FILE).write_text(history_id)


def get_initial_history_id(gmail) -> str:
    profile = gmail.users().getProfile(userId="me").execute()
    return profile["historyId"]


def fetch_new_messages(gmail, start_history_id: str) -> list[str]:
    """Returns list of message IDs added since start_history_id."""
    message_ids = []
    try:
        result = gmail.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            labelId=GMAIL_LABEL,
        ).execute()
        for record in result.get("history", []):
            for msg in record.get("messagesAdded", []):
                message_ids.append(msg["message"]["id"])
        next_history_id = result.get("historyId", start_history_id)
    except HttpError as exc:
        if exc.resp.status == 404:
            # History ID expired; reset
            log.warning("History ID scaduto, reset al più recente.")
            next_history_id = get_initial_history_id(gmail)
            return [], next_history_id
        raise
    return message_ids, next_history_id


def get_message_headers(gmail, msg_id: str) -> list[dict]:
    msg = gmail.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    return msg.get("payload", {}).get("headers", []), msg


# ── Main loop ─────────────────────────────────────────────────────────────────

def process_message(gmail, hs: hubspot.Client, msg_id: str):
    headers, msg = get_message_headers(gmail, msg_id)
    sender = extract_sender(headers)
    if not sender:
        log.debug("Mittente non estraibile per msg %s", msg_id)
        return

    subject = next(
        (h["value"] for h in headers if h["name"].lower() == "subject"), "(nessun oggetto)"
    )
    date_header = next(
        (h["value"] for h in headers if h["name"].lower() == "date"), ""
    )

    status, contact_id = sync_contact(hs, sender)

    print(
        f"[{status.upper()}] "
        f"Email: {sender['email']} | "
        f"ID HubSpot: {contact_id} | "
        f"Oggetto: {subject[:60]}"
    )
    log.info(
        "Processato | status=%s email=%s hubspot_id=%s",
        status, sender["email"], contact_id,
    )

    if status in ("Creato", "Aggiornato") and contact_id != "?":
        add_activity_note(hs, contact_id, subject, date_header)


def run():
    log.info("Avvio Gmail → HubSpot Sync")
    gmail = get_gmail_service()
    hs = get_hubspot_client()

    history_id = load_last_history_id()
    if not history_id:
        history_id = get_initial_history_id(gmail)
        save_history_id(history_id)
        log.info("Primo avvio: history ID iniziale = %s", history_id)
        log.info("In attesa di nuove email…")

    print("\n── Gmail → HubSpot Sync attivo ──────────────────────────────")
    print(f"  Label:    {GMAIL_LABEL}")
    print(f"  Polling:  ogni {POLL_INTERVAL}s")
    print("─────────────────────────────────────────────────────────────\n")

    while True:
        try:
            message_ids, new_history_id = fetch_new_messages(gmail, history_id)
            for msg_id in message_ids:
                try:
                    process_message(gmail, hs, msg_id)
                except Exception as exc:
                    log.error("Errore elaborazione messaggio %s: %s", msg_id, exc)

            if new_history_id != history_id:
                history_id = new_history_id
                save_history_id(history_id)

            if not message_ids:
                log.debug("Nessun nuovo messaggio.")

        except KeyboardInterrupt:
            print("\nInterrotto dall'utente.")
            break
        except Exception as exc:
            log.error("Errore nel ciclo principale: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
