#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed aggiorna/crea automaticamente i contatti in HubSpot.

Uso:
    python gmail_hubspot_sync.py              # loop continuo (ogni 60s)
    python gmail_hubspot_sync.py --once       # singola passata ed esci
    python gmail_hubspot_sync.py --interval 120  # poll ogni 120s
"""

import os
import re
import json
import time
import logging
import argparse
from email.utils import parseaddr
from datetime import datetime, timezone
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"
STATE_FILE = "sync_state.json"
HUBSPOT_API_BASE = "https://api.hubapi.com"

# Domini email personali da cui non derivare il nome azienda
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "aol.com", "protonmail.com",
    "mail.com", "libero.it", "virgilio.it", "tiscali.it",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def get_gmail_service():
    """Autentica con OAuth2 e restituisce il servizio Gmail API."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"File '{CREDENTIALS_FILE}' non trovato. "
                    "Scarica le credenziali OAuth2 dalla Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(gmail_svc, max_results: int = 100) -> list[dict]:
    """Recupera i messaggi dalla INBOX (solo metadati essenziali)."""
    try:
        result = gmail_svc.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=max_results,
        ).execute()
        return result.get("messages", [])
    except HttpError as e:
        logger.error("Errore Gmail API: %s", e)
        return []


def get_message_headers(gmail_svc, msg_id: str) -> dict:
    """Recupera solo gli header From/Subject/Date per un messaggio."""
    msg = gmail_svc.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


# ---------------------------------------------------------------------------
# Parsing mittente
# ---------------------------------------------------------------------------

def extract_sender_info(from_header: str) -> Optional[dict]:
    """
    Estrae email, nome, cognome e azienda dall'header From.
    Ritorna None se l'email non è valida o è il proprio account.
    """
    raw_name, email = parseaddr(from_header)
    if not email or "@" not in email:
        return None

    email = email.lower().strip()
    domain = email.split("@")[1]

    # Ignora noreply e mailer automatici
    local = email.split("@")[0]
    auto_prefixes = {"noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "bounce"}
    if any(local.startswith(p) for p in auto_prefixes):
        return None

    # Nome / cognome
    name = raw_name.strip()
    parts = name.split(None, 1)
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""

    # Azienda dal dominio (escludi domini personali)
    company = ""
    if domain not in PERSONAL_DOMAINS:
        company_raw = domain.split(".")[0]
        company = company_raw.capitalize()

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": name,
        "domain": domain,
        "company": company,
    }


# ---------------------------------------------------------------------------
# Stato persistente
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_ids": set()}


def save_state(state: dict):
    serializable = {**state, "processed_ids": list(state["processed_ids"])}
    with open(STATE_FILE, "w") as f:
        json.dump(serializable, f, indent=2)


def deserialize_state(raw: dict) -> dict:
    raw["processed_ids"] = set(raw.get("processed_ids", []))
    return raw


# ---------------------------------------------------------------------------
# HubSpot client
# ---------------------------------------------------------------------------

class HubSpotClient:
    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    def _url(self, path: str) -> str:
        return f"{HUBSPOT_API_BASE}{path}"

    def search_contact_by_email(self, email: str) -> Optional[dict]:
        """Cerca un contatto per email. Ritorna il record HubSpot o None."""
        payload = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
            "limit": 1,
        }
        r = self.session.post(self._url("/crm/v3/objects/contacts/search"), json=payload)
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        r = self.session.post(self._url("/crm/v3/objects/contacts"), json={"properties": props})
        r.raise_for_status()
        return r.json()

    def update_contact(self, contact_id: str, props: dict) -> dict:
        r = self.session.patch(
            self._url(f"/crm/v3/objects/contacts/{contact_id}"),
            json={"properties": props},
        )
        r.raise_for_status()
        return r.json()

    def create_timeline_note(self, contact_id: str, body: str):
        """Aggiunge una nota sulla timeline del contatto."""
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,
                        }
                    ],
                }
            ],
        }
        r = self.session.post(self._url("/crm/v3/objects/notes"), json=payload)
        if not r.ok:
            logger.warning("Impossibile creare nota timeline per contatto %s: %s", contact_id, r.text)


# ---------------------------------------------------------------------------
# Logica di sincronizzazione per singola email
# ---------------------------------------------------------------------------

def sync_email(hs: HubSpotClient, sender: dict, subject: str, msg_id: str) -> dict:
    """
    Controlla HubSpot e crea o aggiorna il contatto.
    Ritorna un dizionario con stato, email e hubspot_id.
    """
    email = sender["email"]
    existing = hs.search_contact_by_email(email)

    if existing:
        contact_id = existing["id"]
        ex_props = existing.get("properties", {})

        # Aggiorna solo i campi mancanti
        update_props: dict = {}
        if not ex_props.get("firstname") and sender["first_name"]:
            update_props["firstname"] = sender["first_name"]
        if not ex_props.get("lastname") and sender["last_name"]:
            update_props["lastname"] = sender["last_name"]
        if not ex_props.get("company") and sender["company"]:
            update_props["company"] = sender["company"]

        if update_props:
            hs.update_contact(contact_id, update_props)
            status = "Aggiornato"
        else:
            status = "Esistente"

        # Nota timeline
        note_body = (
            f"📨 Email inbound ricevuta\n"
            f"Oggetto: {subject or '(nessun oggetto)'}\n"
            f"Fonte: Gmail | Tag: Inbound Gmail"
        )
        hs.create_timeline_note(contact_id, note_body)

    else:
        new_props: dict = {
            "email": email,
            "lead_source": "Gmail",
        }
        if sender["first_name"]:
            new_props["firstname"] = sender["first_name"]
        if sender["last_name"]:
            new_props["lastname"] = sender["last_name"]
        if sender["company"]:
            new_props["company"] = sender["company"]

        result = hs.create_contact(new_props)
        contact_id = result["id"]
        status = "Creato"

        note_body = (
            f"📨 Primo contatto via email inbound\n"
            f"Oggetto: {subject or '(nessun oggetto)'}\n"
            f"Fonte: Gmail | Tag: Inbound Gmail"
        )
        hs.create_timeline_note(contact_id, note_body)

    return {"status": status, "email": email, "hubspot_id": contact_id}


# ---------------------------------------------------------------------------
# Loop principale
# ---------------------------------------------------------------------------

def run_once(gmail_svc, hs: HubSpotClient, state: dict, max_results: int = 100) -> list[dict]:
    """
    Esegue una singola passata su INBOX e sincronizza i contatti.
    Aggiorna `state` in-place. Ritorna la lista dei risultati.
    """
    messages = fetch_inbox_messages(gmail_svc, max_results=max_results)
    results = []

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in state["processed_ids"]:
            continue

        try:
            headers = get_message_headers(gmail_svc, msg_id)
            from_header = headers.get("From", "")
            subject = headers.get("Subject", "")

            sender = extract_sender_info(from_header)
            if sender is None:
                state["processed_ids"].add(msg_id)
                continue

            result = sync_email(hs, sender, subject, msg_id)
            state["processed_ids"].add(msg_id)
            results.append(result)

            logger.info(
                "%-10s | %-42s | HubSpot ID: %s",
                result["status"],
                result["email"],
                result["hubspot_id"],
            )

        except requests.HTTPError as e:
            logger.error("Errore HTTP per msg %s: %s", msg_id, e)
        except HttpError as e:
            logger.error("Errore Gmail per msg %s: %s", msg_id, e)
        except Exception as e:
            logger.error("Errore imprevisto per msg %s: %s", msg_id, e)

    # Evita crescita illimitata dello stato
    if len(state["processed_ids"]) > 20_000:
        state["processed_ids"] = set(list(state["processed_ids"])[-10_000:])

    return results


def print_summary(results: list[dict]):
    creati = sum(1 for r in results if r["status"] == "Creato")
    aggiornati = sum(1 for r in results if r["status"] == "Aggiornato")
    esistenti = sum(1 for r in results if r["status"] == "Esistente")
    print(f"\nRiepilogo: {creati} creati | {aggiornati} aggiornati | {esistenti} già presenti")


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--interval", type=int, default=60, help="Secondi tra un poll e l'altro (default: 60)")
    parser.add_argument("--once", action="store_true", help="Esegui una sola passata ed esci")
    parser.add_argument("--max-results", type=int, default=100, help="Messaggi da recuperare per passata (max 500)")
    args = parser.parse_args()

    hubspot_token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        raise SystemExit("Errore: variabile d'ambiente HUBSPOT_ACCESS_TOKEN non impostata.")

    gmail_svc = get_gmail_service()
    hs = HubSpotClient(hubspot_token)
    state = deserialize_state(load_state())

    if args.once:
        logger.info("Modalità singola passata.")
        results = run_once(gmail_svc, hs, state, max_results=args.max_results)
        save_state(state)
        print("\n{:<10} | {:<42} | {}".format("Stato", "Email contatto", "ID HubSpot"))
        print("-" * 75)
        for r in results:
            print("{:<10} | {:<42} | {}".format(r["status"], r["email"], r["hubspot_id"]))
        print_summary(results)
        return

    logger.info("Avvio monitoraggio continuo (poll ogni %ds) — premi Ctrl+C per fermare.", args.interval)
    while True:
        try:
            results = run_once(gmail_svc, hs, state, max_results=args.max_results)
            if results:
                save_state(state)
                print_summary(results)
            else:
                logger.info("Nessuna nuova email da processare.")
        except KeyboardInterrupt:
            logger.info("Sync interrotto dall'utente.")
            save_state(state)
            break
        except Exception as e:
            logger.error("Errore nel ciclo principale: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
