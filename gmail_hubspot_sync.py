"""
Gmail → HubSpot Contact Sync
Monitora Gmail in arrivo, estrae i mittenti e li sincronizza in HubSpot.
"""

import os
import json
import base64
import logging
import time
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    ApiException as HubSpotApiException,
)
from hubspot.crm.contacts.models import SimplePublicObjectInput

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configurazione ────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Domini da ignorare (provider email comuni / no-reply)
IGNORED_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "me.com",
    "noreply.com", "no-reply.com", "mailer-daemon.com",
}

IGNORED_LOCAL_PARTS = {"noreply", "no-reply", "mailer-daemon", "bounce", "donotreply"}


# ── Stato persistente ─────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {"last_history_id": None, "processed_message_ids": []}


def save_state(state: dict) -> None:
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)


# ── Autenticazione Gmail ──────────────────────────────────────────────────────
def get_gmail_service():
    creds = None
    if Path(TOKEN_FILE).exists():
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


# ── HubSpot client ────────────────────────────────────────────────────────────
def get_hubspot_client() -> hubspot.Client:
    if not HUBSPOT_API_KEY:
        raise RuntimeError("HUBSPOT_API_KEY non configurata nel file .env")
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


# ── Parsing email ─────────────────────────────────────────────────────────────
def parse_sender(from_header: str) -> tuple[str, str, str, str]:
    """Restituisce (display_name, email, firstname, lastname)."""
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()
    display_name = display_name.strip().strip('"')

    firstname, lastname = "", ""
    if display_name:
        parts = display_name.split(maxsplit=1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""

    return display_name, email_addr, firstname, lastname


def extract_domain(email_addr: str) -> str:
    """Estrae il dominio aziendale dall'email."""
    try:
        return email_addr.split("@")[1]
    except IndexError:
        return ""


def domain_to_company(domain: str) -> str:
    """Converte il dominio in nome azienda best-effort (es. acme.com → Acme)."""
    base = domain.split(".")[0]
    return base.capitalize()


def is_ignored(email_addr: str) -> bool:
    if not email_addr or "@" not in email_addr:
        return True
    local, domain = email_addr.split("@", 1)
    return domain in IGNORED_DOMAINS or local in IGNORED_LOCAL_PARTS


# ── HubSpot: cerca / crea / aggiorna ─────────────────────────────────────────
def find_contact_by_email(hs: hubspot.Client, email: str) -> dict | None:
    """Cerca un contatto per email; restituisce il record HubSpot o None."""
    try:
        resp = hs.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": [
                    "email", "firstname", "lastname", "company", "leadsource"
                ],
                "limit": 1,
            }
        )
        if resp.total > 0:
            return resp.results[0]
    except HubSpotApiException as e:
        log.error("Errore ricerca HubSpot: %s", e)
    return None


def build_contact_properties(
    email: str,
    firstname: str,
    lastname: str,
    company: str,
) -> dict:
    props = {
        "email": email,
        "leadsource": "Gmail",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def create_contact(hs: hubspot.Client, props: dict) -> str | None:
    """Crea un nuovo contatto; restituisce l'ID o None in caso di errore."""
    try:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result.id
    except HubSpotApiException as e:
        log.error("Errore creazione contatto: %s", e)
    return None


def update_contact(hs: hubspot.Client, contact_id: str, existing: dict, new_props: dict) -> bool:
    """Aggiorna solo i campi mancanti nel contatto esistente."""
    existing_props = existing.properties if hasattr(existing, "properties") else {}
    update_props = {}
    for key, value in new_props.items():
        if key == "email":
            continue  # non sovrascrivere la chiave
        if not existing_props.get(key) and value:
            update_props[key] = value

    if not update_props:
        return False  # niente da aggiornare

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=update_props),
        )
        return True
    except HubSpotApiException as e:
        log.error("Errore aggiornamento contatto %s: %s", contact_id, e)
    return False


def add_activity_note(hs: hubspot.Client, contact_id: str, subject: str, email_date: str) -> None:
    """Registra una timeline activity (nota email ricevuta) sul contatto."""
    try:
        hs.crm.timeline.events_api  # verifica disponibilità
    except Exception:
        pass  # timeline non disponibile in tutti i piani

    try:
        note_body = f"Email inbound ricevuta il {email_date}\nOggetto: {subject}\nTag: Inbound Gmail"
        hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={
                    "hs_note_body": note_body,
                    "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                },
                associations=[
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}
                        ],
                    }
                ],
            )
        )
    except Exception as e:
        log.warning("Impossibile aggiungere nota attività: %s", e)


# ── Processamento email ───────────────────────────────────────────────────────
def process_message(
    gmail_svc,
    hs: hubspot.Client,
    msg_id: str,
) -> dict:
    """Processa un singolo messaggio e sincronizza il contatto in HubSpot.

    Ritorna un dict con: status, email, contact_id.
    """
    result = {"message_id": msg_id, "status": "Ignorato", "email": "", "contact_id": ""}

    try:
        msg = gmail_svc.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Date", "Subject"]
        ).execute()
    except HttpError as e:
        log.error("Errore recupero messaggio %s: %s", msg_id, e)
        return result

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    from_header = headers.get("From", "")
    subject = headers.get("Subject", "(senza oggetto)")
    date_header = headers.get("Date", "")

    display_name, email_addr, firstname, lastname = parse_sender(from_header)
    result["email"] = email_addr

    if is_ignored(email_addr):
        log.info("Ignorato: %s (mittente filtrato)", email_addr)
        return result

    domain = extract_domain(email_addr)
    company = domain_to_company(domain) if domain not in IGNORED_DOMAINS else ""

    props = build_contact_properties(email_addr, firstname, lastname, company)

    existing = find_contact_by_email(hs, email_addr)

    if existing:
        contact_id = existing.id
        updated = update_contact(hs, contact_id, existing, props)
        result["status"] = "Aggiornato" if updated else "Ignorato"
        result["contact_id"] = contact_id
        log.info(
            "%s | email=%s | id=%s",
            result["status"], email_addr, contact_id
        )
    else:
        contact_id = create_contact(hs, props)
        if contact_id:
            result["status"] = "Creato"
            result["contact_id"] = contact_id
            log.info("Creato  | email=%s | id=%s", email_addr, contact_id)
        else:
            result["status"] = "Errore"

    if result["contact_id"] and result["status"] in ("Creato", "Aggiornato"):
        add_activity_note(hs, result["contact_id"], subject, date_header)

    return result


# ── Fetch nuove email ─────────────────────────────────────────────────────────
def fetch_new_messages(gmail_svc, state: dict) -> list[str]:
    """Recupera gli ID dei nuovi messaggi in arrivo non ancora processati."""
    processed = set(state.get("processed_message_ids", []))
    new_ids = []

    try:
        response = gmail_svc.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            q="is:unread",
            maxResults=50,
        ).execute()
    except HttpError as e:
        log.error("Errore listing messaggi Gmail: %s", e)
        return []

    messages = response.get("messages", [])
    for m in messages:
        mid = m["id"]
        if mid not in processed:
            new_ids.append(mid)

    return new_ids


# ── Loop principale ───────────────────────────────────────────────────────────
def run_sync_loop() -> None:
    log.info("Avvio sincronizzazione Gmail → HubSpot")
    log.info("Intervallo polling: %ds", POLL_INTERVAL)

    gmail_svc = get_gmail_service()
    hs = get_hubspot_client()
    state = load_state()

    cycle = 0
    while True:
        cycle += 1
        log.info("── Ciclo #%d ─────────────────────────────", cycle)

        new_ids = fetch_new_messages(gmail_svc, state)

        if not new_ids:
            log.info("Nessun nuovo messaggio.")
        else:
            log.info("Trovati %d nuovi messaggi da processare.", len(new_ids))
            for msg_id in new_ids:
                result = process_message(gmail_svc, hs, msg_id)
                print(
                    f"  [{result['status']:10s}] email={result['email'] or 'N/A'}"
                    f"  contact_id={result['contact_id'] or 'N/A'}"
                )
                state["processed_message_ids"].append(msg_id)
                # Mantieni solo gli ultimi 10000 ID per evitare crescita illimitata
                state["processed_message_ids"] = state["processed_message_ids"][-10000:]

            save_state(state)

        log.info("Attendo %ds prima del prossimo ciclo...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_sync_loop()
