#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora la casella Gmail, estrae i mittenti e li sincronizza in HubSpot.
Evita duplicati usando l'email come chiave unica.
"""

import os
import sys
import json
import time
import logging
import pickle
import re
from datetime import datetime
from email.utils import parseaddr
from typing import Optional

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync.log"),
    ],
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.pickle")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE = "https://api.hubapi.com"

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
STATE_FILE = "sync_state.json"

FREE_EMAIL_PROVIDERS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "icloud.com", "live.com", "live.it",
    "msn.com", "aol.com", "libero.it", "alice.it", "tiscali.it",
    "virgilio.it", "tin.it", "fastwebnet.it", "protonmail.com",
    "pm.me", "tutanota.com", "gmx.com", "gmx.net", "yandex.com",
}

SKIP_SENDERS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications",
    "newsletter", "info@", "support@", "help@", "contact@",
    "admin@", "system@", "automated@",
}


# ---------------------------------------------------------------------------
# Gmail Client
# ---------------------------------------------------------------------------

class GmailClient:
    def __init__(self):
        self.service = self._build_service()

    def _build_service(self):
        creds = None
        if os.path.exists(GMAIL_TOKEN_FILE):
            with open(GMAIL_TOKEN_FILE, "rb") as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(GMAIL_CREDENTIALS_FILE):
                    raise FileNotFoundError(
                        f"File credenziali Gmail non trovato: {GMAIL_CREDENTIALS_FILE}\n"
                        "Scarica il file OAuth2 da Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(GMAIL_TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)
            log.info("Credenziali Gmail salvate in %s", GMAIL_TOKEN_FILE)

        return build("gmail", "v1", credentials=creds)

    def list_inbox_messages(self, max_results: int = 50) -> list[dict]:
        try:
            result = (
                self.service.users()
                .messages()
                .list(userId="me", q="in:inbox -from:me", maxResults=max_results)
                .execute()
            )
            return result.get("messages", [])
        except HttpError as e:
            log.error("Errore lista messaggi Gmail: %s", e)
            return []

    def get_message_headers(self, message_id: str) -> dict:
        try:
            msg = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            headers["_internal_date"] = msg.get("internalDate", "")
            return headers
        except HttpError as e:
            log.error("Errore lettura messaggio %s: %s", message_id, e)
            return {}

    def get_profile(self) -> dict:
        return self.service.users().getProfile(userId="me").execute()


# ---------------------------------------------------------------------------
# HubSpot Client
# ---------------------------------------------------------------------------

class HubSpotClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("HUBSPOT_API_KEY non impostato nel file .env")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        r = requests.post(f"{HUBSPOT_BASE}{path}", headers=self.headers, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, payload: dict) -> dict:
        r = requests.patch(f"{HUBSPOT_BASE}{path}", headers=self.headers, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        payload = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source"],
            "limit": 1,
        }
        data = self._post("/crm/v3/contacts/search", payload)
        if data.get("total", 0) > 0:
            return data["results"][0]
        return None

    def create_contact(self, properties: dict) -> dict:
        return self._post("/crm/v3/contacts", {"properties": properties})

    def update_contact(self, contact_id: int, properties: dict) -> dict:
        return self._patch(f"/crm/v3/contacts/{contact_id}", {"properties": properties})

    def log_email_activity(self, contact_id: str, sender_email: str, subject: str, timestamp_ms: str):
        payload = {
            "properties": {
                "hs_timestamp": timestamp_ms,
                "hs_email_direction": "INCOMING_EMAIL",
                "hs_email_status": "RECEIVED",
                "hs_email_subject": subject or "(senza oggetto)",
                "hs_email_html": f"Email in arrivo da {sender_email}",
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 198}
                    ],
                }
            ],
        }
        try:
            self._post("/crm/v3/objects/emails", payload)
        except Exception as e:
            log.warning("Impossibile registrare attività timeline per %s: %s", contact_id, e)


# ---------------------------------------------------------------------------
# Sync Engine
# ---------------------------------------------------------------------------

class SyncEngine:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient):
        self.gmail = gmail
        self.hubspot = hubspot
        self.processed_ids: set[str] = set()
        self._load_state()

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
            self.processed_ids = set(state.get("processed_ids", []))
            log.info("Stato caricato: %d messaggi già processati", len(self.processed_ids))

    def _save_state(self):
        ids_list = list(self.processed_ids)[-20_000:]
        with open(STATE_FILE, "w") as f:
            json.dump({"processed_ids": ids_list}, f)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_sender(self, from_header: str) -> tuple[str, str]:
        """Ritorna (nome, email) oppure ('', '') se da saltare."""
        name, email = parseaddr(from_header)
        if not email or "@" not in email:
            return "", ""
        email = email.lower().strip()

        if any(skip in email for skip in SKIP_SENDERS):
            return "", ""

        # Salta indirizzi con "noreply", "newsletter" ecc. nel nome utente
        local_part = email.split("@")[0]
        skip_local = {"noreply", "no-reply", "donotreply", "newsletter",
                      "newsletter", "mailer-daemon", "postmaster", "automated"}
        if local_part in skip_local:
            return "", ""

        return name.strip(), email

    def _split_name(self, full_name: str) -> tuple[str, str]:
        parts = full_name.strip().split(maxsplit=1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return parts[0] if parts else "", ""

    def _company_from_domain(self, email: str) -> tuple[str, str]:
        """Ritorna (nome_azienda, dominio). nome_azienda è '' per provider gratuiti."""
        domain = email.split("@")[-1].lower()
        if domain in FREE_EMAIL_PROVIDERS:
            return "", domain
        # "mayaamenduni.com" → "Mayaamenduni"
        company = domain.split(".")[0].capitalize()
        return company, domain

    # ------------------------------------------------------------------
    # Core sync logic
    # ------------------------------------------------------------------

    def sync_contact(self, from_header: str, subject: str, timestamp_ms: str) -> dict:
        name, email = self._parse_sender(from_header)

        if not email:
            return {"status": "Ignorato", "motivo": "Mittente automatico/no-reply", "email": ""}

        firstname, lastname = self._split_name(name)
        company, domain = self._company_from_domain(email)

        existing = self.hubspot.find_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            props = existing.get("properties", {})

            updates: dict[str, str] = {}
            if firstname and not props.get("firstname"):
                updates["firstname"] = firstname
            if lastname and not props.get("lastname"):
                updates["lastname"] = lastname
            if company and not props.get("company"):
                updates["company"] = company

            if updates:
                self.hubspot.update_contact(int(contact_id), updates)
                self.hubspot.log_email_activity(contact_id, email, subject, timestamp_ms)
                return {
                    "status": "Aggiornato",
                    "email": email,
                    "contact_id": contact_id,
                    "campi_aggiornati": list(updates.keys()),
                }

            self.hubspot.log_email_activity(contact_id, email, subject, timestamp_ms)
            return {
                "status": "Ignorato",
                "motivo": "Già sincronizzato, nessun campo da aggiornare",
                "email": email,
                "contact_id": contact_id,
            }

        # Nuovo contatto
        properties: dict[str, str] = {
            "email": email,
            "hs_analytics_source": "EMAIL_MARKETING",  # HubSpot enum più vicino a "Gmail"
        }
        if firstname:
            properties["firstname"] = firstname
        if lastname:
            properties["lastname"] = lastname
        if company:
            properties["company"] = company

        result = self.hubspot.create_contact(properties)
        contact_id = result["id"]

        self.hubspot.log_email_activity(contact_id, email, subject, timestamp_ms)
        return {"status": "Creato", "email": email, "contact_id": contact_id}

    def process_message(self, message_id: str) -> Optional[dict]:
        if message_id in self.processed_ids:
            return None

        headers = self.gmail.get_message_headers(message_id)
        if not headers:
            self.processed_ids.add(message_id)
            return None

        from_header = headers.get("From", "")
        subject = headers.get("Subject", "")
        timestamp_ms = headers.get("_internal_date", str(int(datetime.utcnow().timestamp() * 1000)))

        result = self.sync_contact(from_header, subject, timestamp_ms)
        result["message_id"] = message_id
        self.processed_ids.add(message_id)
        return result

    # ------------------------------------------------------------------
    # Run modes
    # ------------------------------------------------------------------

    def run_once(self, max_messages: int = 50) -> list[dict]:
        messages = self.gmail.list_inbox_messages(max_results=max_messages)
        results = []
        for msg in messages:
            r = self.process_message(msg["id"])
            if r:
                results.append(r)
        self._save_state()
        return results

    def run_continuous(self, interval: int = CHECK_INTERVAL):
        log.info("=== Gmail → HubSpot Sync avviato (intervallo: %ds) ===", interval)
        while True:
            log.info("--- Ciclo di sync ---")
            try:
                results = self.run_once()
                _print_results(results)
            except KeyboardInterrupt:
                log.info("Sync interrotto dall'utente.")
                break
            except Exception as e:
                log.error("Errore nel ciclo di sync: %s", e)
            time.sleep(interval)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _print_results(results: list[dict]):
    if not results:
        log.info("Nessuna nuova email da processare.")
        return

    print("\n" + "=" * 72)
    print(f"{'STATO':<32} {'EMAIL':<36} {'ID HubSpot'}")
    print("-" * 72)
    for r in results:
        stato = r["status"]
        if r.get("campi_aggiornati"):
            stato += f" ({', '.join(r['campi_aggiornati'])})"
        email = r.get("email", "—")
        cid = r.get("contact_id", "—")
        print(f"{stato:<32} {email:<36} {cid}")
    print("=" * 72)
    print(
        f"Totale: {len(results)} | "
        f"Creati: {sum(1 for r in results if r['status']=='Creato')} | "
        f"Aggiornati: {sum(1 for r in results if r['status']=='Aggiornato')} | "
        f"Ignorati: {sum(1 for r in results if r['status']=='Ignorato')}"
    )
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--once", action="store_true",
        help="Esegui una sola scansione e termina"
    )
    parser.add_argument(
        "--interval", type=int, default=CHECK_INTERVAL,
        help=f"Secondi tra un ciclo e l'altro (default: {CHECK_INTERVAL})"
    )
    parser.add_argument(
        "--max", type=int, default=50,
        help="Numero massimo di email da leggere per ciclo (default: 50)"
    )
    args = parser.parse_args()

    gmail = GmailClient()
    hubspot = HubSpotClient(HUBSPOT_API_KEY)
    engine = SyncEngine(gmail, hubspot)

    if args.once:
        results = engine.run_once(max_messages=args.max)
        _print_results(results)
    else:
        engine.run_continuous(interval=args.interval)


if __name__ == "__main__":
    main()
