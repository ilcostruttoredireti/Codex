#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora la casella Gmail, estrae i mittenti e li sincronizza in HubSpot.
Usa l'email come chiave univoca per evitare duplicati.
"""

import os
import re
import json
import time
import logging
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv

# Gmail API
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# HubSpot API
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(".sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Domini personali da escludere per la derivazione del nome azienda
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "pm.me", "fastmail.com", "zoho.com", "yandex.com", "gmx.com",
    "libero.it", "tiscali.it", "virgilio.it", "alice.it",
}

# Pattern per rilevare mittenti automatici da ignorare
NOREPLY_PATTERNS = [
    r"no.?reply", r"noreply", r"donotreply", r"do.not.reply",
    r"^notifications?@", r"mailer-daemon", r"postmaster@",
    r"^bounce[s+]?@", r"^auto-?confirm", r"^support-ticket",
]


# ─── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """Crea e restituisce il client Gmail con OAuth2."""
    creds = None
    token_path = Path("token.json")

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
            if not Path(credentials_file).exists():
                raise FileNotFoundError(
                    f"File credenziali Gmail non trovato: {credentials_file}\n"
                    "Scarica 'credentials.json' da Google Cloud Console e "
                    "impostalo nella variabile GMAIL_CREDENTIALS_FILE."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_new_message_ids(service, last_history_id: str | None, processed_ids: set) -> tuple[list[str], str]:
    """
    Restituisce gli ID dei nuovi messaggi in arrivo e il nuovo historyId.
    Al primo avvio cerca le email recenti (ultimi 2 giorni).
    Alle iterazioni successive usa la Gmail History API per gli incrementali.
    """
    new_ids: list[str] = []
    new_history_id = last_history_id

    if last_history_id:
        try:
            history_resp = service.users().history().list(
                userId="me",
                startHistoryId=last_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()

            new_history_id = history_resp.get("historyId", last_history_id)

            for record in history_resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    if msg["id"] not in processed_ids:
                        if "INBOX" in msg.get("labelIds", []):
                            new_ids.append(msg["id"])
            return new_ids, new_history_id

        except HttpError as e:
            if e.resp.status == 404:
                # historyId scaduto → reset e riparti dalla ricerca
                logger.warning("History ID scaduto, reset al polling completo.")
                last_history_id = None
            else:
                raise

    # Prima esecuzione o dopo reset: recupera le ultime email
    result = service.users().messages().list(
        userId="me",
        q="in:inbox -in:sent newer_than:2d",
        maxResults=50,
    ).execute()

    for msg in result.get("messages", []):
        if msg["id"] not in processed_ids:
            new_ids.append(msg["id"])

    profile = service.users().getProfile(userId="me").execute()
    new_history_id = profile.get("historyId")

    return new_ids, new_history_id


def extract_sender(service, message_id: str) -> dict | None:
    """
    Legge gli header del messaggio e restituisce un dict con i dati del mittente,
    oppure None se il mittente va ignorato.
    """
    try:
        msg = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "(nessun oggetto)")

        display_name, email = parseaddr(from_header)
        email = email.lower().strip()

        if not email or "@" not in email:
            return None

        # Salta la propria casella
        own_email = os.getenv("MY_EMAIL", "").lower()
        if own_email and email == own_email:
            return None

        # Salta mittenti automatici
        if any(re.search(p, email, re.IGNORECASE) for p in NOREPLY_PATTERNS):
            logger.debug("Mittente automatico ignorato: %s", email)
            return None

        domain = email.split("@")[1]

        return {
            "email": email,
            "display_name": display_name.strip(),
            "domain": domain,
            "subject": subject,
            "message_id": message_id,
        }

    except HttpError as e:
        logger.error("Errore lettura messaggio %s: %s", message_id, e)
        return None


# ─── Parsing nome / azienda ────────────────────────────────────────────────────

def split_name(display_name: str) -> tuple[str, str]:
    """Divide il nome visualizzato in (firstname, lastname)."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return display_name.strip(), ""


def company_from_domain(domain: str) -> str:
    """Deriva un nome azienda dal dominio email; stringa vuota per domini personali."""
    if domain.lower() in PERSONAL_DOMAINS:
        return ""
    # Rimuove TLD e capitalizza: "acmecorp.com" → "Acmecorp"
    name = domain.split(".")[0]
    return name.capitalize()


# ─── HubSpot ───────────────────────────────────────────────────────────────────

def get_hubspot_client():
    token = os.getenv("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        raise ValueError(
            "HUBSPOT_PRIVATE_APP_TOKEN non impostato.\n"
            "Crea un Private App in HubSpot Settings → Integrations → Private Apps."
        )
    return hubspot.Client.create(access_token=token)


def find_contact(hs_client, email: str) -> dict | None:
    """Cerca un contatto HubSpot per email; restituisce {'id', 'properties'} o None."""
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
        ],
        properties=["email", "firstname", "lastname", "company"],
        limit=1,
    )
    try:
        resp = hs_client.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        if resp.results:
            c = resp.results[0]
            return {"id": c.id, "properties": c.properties}
    except ApiException as e:
        logger.error("Errore ricerca HubSpot (%s): %s", email, e)
    return None


def create_contact(hs_client, sender: dict) -> str | None:
    """Crea un nuovo contatto HubSpot. Restituisce il contact ID o None."""
    firstname, lastname = split_name(sender["display_name"]) if sender["display_name"] else ("", "")
    company = company_from_domain(sender["domain"])

    props: dict[str, str] = {"email": sender["email"]}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    # Campi standard HubSpot per tracciare la sorgente
    props["hs_analytics_source"] = "OTHER_CAMPAIGNS"
    props["hs_analytics_source_data_1"] = "Gmail"
    props["hs_analytics_source_data_2"] = "Inbound Gmail"

    try:
        obj = hs_client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return obj.id
    except ApiException as e:
        logger.error("Errore creazione contatto %s: %s", sender["email"], e)
        return None


def update_contact(hs_client, contact_id: str, sender: dict, existing_props: dict) -> bool:
    """Aggiorna i campi mancanti su un contatto esistente. Restituisce True se ha aggiornato."""
    updates: dict[str, str] = {}

    firstname, lastname = split_name(sender["display_name"]) if sender["display_name"] else ("", "")
    company = company_from_domain(sender["domain"])

    if firstname and not existing_props.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not existing_props.get("lastname"):
        updates["lastname"] = lastname
    if company and not existing_props.get("company"):
        updates["company"] = company

    if not updates:
        return False

    try:
        hs_client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as e:
        logger.error("Errore aggiornamento contatto %s: %s", contact_id, e)
        return False


# ─── Sincronizzazione ─────────────────────────────────────────────────────────

def sync_sender(hs_client, sender: dict) -> tuple[str, str]:
    """
    Crea o aggiorna il contatto HubSpot per il mittente.
    Restituisce (stato, contact_id) dove stato è: Creato / Aggiornato / Ignorato / Errore
    """
    existing = find_contact(hs_client, sender["email"])

    if existing:
        updated = update_contact(hs_client, existing["id"], sender, existing["properties"])
        status = "Aggiornato" if updated else "Ignorato"
        return status, existing["id"]

    contact_id = create_contact(hs_client, sender)
    if contact_id:
        return "Creato", contact_id
    return "Errore", ""


# ─── Stato persistente ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {"history_id": None, "processed_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── Loop principale ───────────────────────────────────────────────────────────

def run():
    logger.info("=== Gmail → HubSpot Sync avviato (polling ogni %ds) ===", POLL_INTERVAL)

    gmail = get_gmail_service()
    hs = get_hubspot_client()

    state = load_state()
    processed_ids: set[str] = set(state.get("processed_ids", []))
    history_id: str | None = state.get("history_id")

    logger.info("Stato caricato: %d messaggi già processati", len(processed_ids))

    while True:
        try:
            logger.info("Controllo nuove email in arrivo...")
            message_ids, history_id = fetch_new_message_ids(gmail, history_id, processed_ids)

            if not message_ids:
                logger.info("Nessuna nuova email.")
            else:
                logger.info("%d nuova/e email da processare", len(message_ids))
                _print_header()

                for msg_id in message_ids:
                    sender = extract_sender(gmail, msg_id)

                    if sender:
                        status, contact_id = sync_sender(hs, sender)
                        _print_row(status, sender["email"], contact_id)
                    else:
                        logger.debug("Messaggio %s saltato", msg_id)

                    processed_ids.add(msg_id)

                _print_footer()

                # Limita la dimensione del set (mantieni gli ultimi 10.000)
                if len(processed_ids) > 10_000:
                    processed_ids = set(list(processed_ids)[-5_000:])

                save_state({"history_id": history_id, "processed_ids": list(processed_ids)})

        except KeyboardInterrupt:
            logger.info("Sync interrotto dall'utente. Salvataggio stato...")
            save_state({"history_id": history_id, "processed_ids": list(processed_ids)})
            break
        except Exception as exc:
            logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        logger.info("Prossimo controllo tra %ds...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


def _print_header():
    print(f"\n{'─'*65}")
    print(f"{'STATO':<13} {'EMAIL CONTATTO':<36} {'ID HUBSPOT'}")
    print(f"{'─'*65}")


def _print_row(status: str, email: str, contact_id: str):
    print(f"{status:<13} {email:<36} {contact_id}")


def _print_footer():
    print(f"{'─'*65}\n")


if __name__ == "__main__":
    run()
