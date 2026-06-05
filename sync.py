#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora la casella Gmail e sincronizza automaticamente i mittenti come contatti in HubSpot.

Utilizzo:
    python sync.py              # loop continuo
    python sync.py --once       # esecuzione singola e uscita
"""

import argparse
import json
import logging
import os
import re
import time
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts import ApiException as ContactsApiException

load_dotenv()

# ── Configurazione ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
STATE_FILE = Path(os.getenv("STATE_FILE", "state/processed_ids.json"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MAX_RESULTS = int(os.getenv("MAX_RESULTS_PER_POLL", "50"))
ENABLE_NOTES = os.getenv("ENABLE_TIMELINE_NOTES", "true").lower() == "true"

# Domini email pubblici — il dominio non viene usato come azienda
PUBLIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "outlook.com",
    "hotmail.com", "hotmail.it", "live.com", "live.it", "icloud.com",
    "me.com", "mac.com", "protonmail.com", "proton.me", "fastmail.com",
    "fastmail.fm", "aol.com", "msn.com", "libero.it", "virgilio.it",
    "tiscali.it", "alice.it", "tin.it", "email.it", "inwind.it",
    "zoho.com", "mail.com", "inbox.com", "yandex.com", "yandex.ru",
}

# Mittenti automatici da ignorare
SKIP_PATTERN = re.compile(
    r"^(noreply|no-?reply|do-?not-?reply|mailer-?daemon|postmaster|"
    r"notifications?|alerts?|auto-?reply|bounce|newsletter|news|"
    r"unsubscribe|marketing|info|support|help|contact|admin)@",
    re.IGNORECASE,
)

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail ──────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """Autenticazione Gmail con OAuth2 e ritorna il servizio API."""
    creds = None
    token_path = Path(GMAIL_TOKEN_FILE)
    creds_path = Path(GMAIL_CREDENTIALS_FILE)

    if not creds_path.exists():
        raise FileNotFoundError(
            f"File credenziali Gmail non trovato: {GMAIL_CREDENTIALS_FILE}\n"
            "Scaricalo dalla Google Cloud Console → API & Services → Credentials"
        )

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service):
    """Restituisce la lista di stub dei messaggi in INBOX."""
    result = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=MAX_RESULTS)
        .execute()
    )
    return result.get("messages", [])


def get_message_metadata(service, msg_id: str) -> dict:
    """Recupera solo gli header necessari di un messaggio."""
    msg = (
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
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


# ── Parsing mittente ───────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> dict | None:
    """
    Estrae email, nome, cognome e azienda dall'header From.
    Ritorna None se l'email non è valida.
    """
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()

    if not email or "@" not in email:
        return None

    # Nome / cognome dal display name
    name_parts = display_name.strip().split() if display_name.strip() else []
    firstname = name_parts[0] if name_parts else ""
    lastname = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    # Azienda dal dominio (solo domini non pubblici)
    domain = email.split("@")[-1]
    company = ""
    if domain not in PUBLIC_DOMAINS:
        # "mail.acme.co.uk" → "Acme", "acme.com" → "Acme"
        labels = domain.split(".")
        raw = labels[-2] if len(labels) >= 2 else labels[0]
        company = raw.replace("-", " ").title()

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


# ── Gestione stato ─────────────────────────────────────────────────────────────

def load_state() -> set:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(processed: set):
    STATE_FILE.write_text(json.dumps(sorted(processed), indent=2))


# ── HubSpot ────────────────────────────────────────────────────────────────────

def build_hs_client():
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact(hs_client, email: str):
    """Cerca un contatto per email. Ritorna il record o None."""
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email)]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    resp = hs_client.crm.contacts.search_api.do_search(
        public_object_search_request=search_req
    )
    return resp.results[0] if resp.total > 0 else None


def create_contact(hs_client, sender: dict) -> str:
    """Crea un nuovo contatto in HubSpot. Ritorna il contact_id."""
    props = {
        "email": sender["email"],
        "hs_lead_status": "NEW",
        "leadsource": "Gmail",
    }
    if sender["firstname"]:
        props["firstname"] = sender["firstname"]
    if sender["lastname"]:
        props["lastname"] = sender["lastname"]
    if sender["company"]:
        props["company"] = sender["company"]

    result = hs_client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
    )
    return result.id


def update_contact(hs_client, contact_id: str, existing, sender: dict) -> bool:
    """
    Aggiorna i campi mancanti di un contatto esistente.
    Ritorna True se è stato effettivamente aggiornato.
    """
    ep = existing.properties or {}
    updates = {}

    if sender["firstname"] and not ep.get("firstname"):
        updates["firstname"] = sender["firstname"]
    if sender["lastname"] and not ep.get("lastname"):
        updates["lastname"] = sender["lastname"]
    if sender["company"] and not ep.get("company"):
        updates["company"] = sender["company"]
    if not ep.get("leadsource"):
        updates["leadsource"] = "Gmail"

    if not updates:
        return False

    hs_client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=SimplePublicObjectInput(properties=updates),
    )
    return True


def add_note(hs_client, contact_id: str, sender: dict, subject: str):
    """Aggiunge una nota di timeline al contatto (email ricevuta)."""
    body_text = (
        f"Email inbound ricevuta da Gmail.\n"
        f"Mittente: {sender['firstname']} {sender['lastname']}".strip()
        + f" <{sender['email']}>\n"
        f"Oggetto: {subject}\n"
        f"Tag: Inbound Gmail"
    )
    note_props = {
        "hs_note_body": body_text,
        "hs_timestamp": str(int(time.time() * 1000)),
    }
    from hubspot.crm.objects.notes.models import SimplePublicObjectInputForCreate as NoteCreate

    note = NoteCreate(
        properties=note_props,
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
        hs_client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note
        )
    except Exception as e:
        log.debug("Impossibile creare nota per %s: %s", contact_id, e)


# ── Sincronizzazione ───────────────────────────────────────────────────────────

def sync_batch(gmail_service, hs_client, processed: set) -> list[dict]:
    """
    Elabora i messaggi in arrivo non ancora processati.
    Ritorna la lista dei risultati per questo ciclo.
    """
    messages = fetch_inbox_messages(gmail_service)
    results = []

    for stub in messages:
        msg_id = stub["id"]
        if msg_id in processed:
            continue

        # Recupera header
        try:
            headers = get_message_metadata(gmail_service, msg_id)
        except Exception as exc:
            log.warning("Impossibile recuperare il messaggio %s: %s", msg_id, exc)
            processed.add(msg_id)
            continue

        from_header = headers.get("From", "")
        subject = headers.get("Subject", "(senza oggetto)")

        if not from_header:
            processed.add(msg_id)
            continue

        sender = parse_sender(from_header)

        # Email non valida
        if sender is None:
            processed.add(msg_id)
            continue

        # Mittente automatico
        if SKIP_PATTERN.match(sender["email"]):
            log.debug("Saltato mittente automatico: %s", sender["email"])
            processed.add(msg_id)
            continue

        # Sincronizzazione HubSpot
        try:
            existing = find_contact(hs_client, sender["email"])

            if existing is None:
                contact_id = create_contact(hs_client, sender)
                status = "Creato"
            else:
                contact_id = existing.id
                updated = update_contact(hs_client, contact_id, existing, sender)
                status = "Aggiornato" if updated else "Ignorato"

            if ENABLE_NOTES and status != "Ignorato":
                add_note(hs_client, contact_id, sender, subject)

        except ContactsApiException as exc:
            log.error("Errore HubSpot per %s: %s", sender["email"], exc)
            continue  # riprova al prossimo ciclo

        processed.add(msg_id)

        record = {
            "stato": status,
            "email": sender["email"],
            "hubspot_id": contact_id,
            "oggetto": subject,
        }
        results.append(record)

        icon = {"Creato": "✚", "Aggiornato": "↑", "Ignorato": "–"}.get(status, "?")
        log.info(
            "%s %-10s | %-40s | HubSpot ID: %s",
            icon, status, sender["email"], contact_id,
        )

    return results


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui un singolo ciclo e termina (utile per cron)",
    )
    args = parser.parse_args()

    if not HUBSPOT_ACCESS_TOKEN:
        raise SystemExit(
            "Errore: HUBSPOT_ACCESS_TOKEN non configurato.\n"
            "Copia .env.example in .env e inserisci il token."
        )

    log.info("Connessione a Gmail…")
    gmail_service = get_gmail_service()
    hs_client = build_hs_client()
    processed = load_state()

    log.info(
        "Avvio monitoraggio Gmail → HubSpot | intervallo=%ds | note=%s",
        POLL_INTERVAL, ENABLE_NOTES,
    )
    log.info("Messaggi già processati in cache: %d", len(processed))

    try:
        while True:
            log.info("── Controllo nuove email ──")
            results = sync_batch(gmail_service, hs_client, processed)
            save_state(processed)

            if results:
                creati = sum(1 for r in results if r["stato"] == "Creato")
                aggiornati = sum(1 for r in results if r["stato"] == "Aggiornato")
                ignorati = sum(1 for r in results if r["stato"] == "Ignorato")
                log.info(
                    "Ciclo completato → Creati: %d | Aggiornati: %d | Ignorati: %d",
                    creati, aggiornati, ignorati,
                )
            else:
                log.info("Nessuna nuova email da processare.")

            if args.once:
                break

            log.info("Prossimo controllo tra %d secondi…", POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("Interruzione manuale. Stato salvato.")
        save_state(processed)


if __name__ == "__main__":
    main()
