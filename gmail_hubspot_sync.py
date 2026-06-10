#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed esegue l'upsert dei mittenti in HubSpot.

Configurazione:
  Crea un file .env (o esporta le variabili) con:
    HUBSPOT_ACCESS_TOKEN=...
    GMAIL_CREDENTIALS_FILE=credentials.json   # OAuth2 desktop app
    GMAIL_TOKEN_FILE=token.json               # Generato al primo avvio
    POLL_INTERVAL_SECONDS=60                  # Default 60
    STATE_FILE=processed_messages.txt         # IDs già elaborati
"""

import os
import re
import sys
import time
import json
import logging
import email.utils
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GRequest
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

# ── Configurazione ────────────────────────────────────────────────────────────
HUBSPOT_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "processed_messages.txt"))

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

SKIP_SENDERS = {
    "mailer-daemon", "postmaster", "noreply", "no-reply",
    "notifications", "do-not-reply", "donotreply", "auto-confirm",
    "facebookmail.com", "notification@", "analytics-noreply",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail ─────────────────────────────────────────────────────────────────────
def gmail_service():
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def load_processed() -> set[str]:
    if STATE_FILE.exists():
        return set(STATE_FILE.read_text().splitlines())
    return set()


def save_processed(ids: set[str]) -> None:
    STATE_FILE.write_text("\n".join(sorted(ids)))


def fetch_new_messages(service, processed: set[str]) -> list[dict]:
    result = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], q="-from:me")
        .execute()
    )
    messages = result.get("messages", [])
    new = [m for m in messages if m["id"] not in processed]
    full = []
    for m in new:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=m["id"], format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        full.append(msg)
    return full


def parse_sender(headers: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Ritorna (email, display_name) dall'header From."""
    for h in headers:
        if h["name"].lower() == "from":
            name, addr = email.utils.parseaddr(h["value"])
            return addr.lower().strip(), name.strip() or None
    return None, None


def extract_name_parts(display_name: Optional[str]) -> tuple[str, str]:
    """Cerca di estrarre firstname/lastname da un display name."""
    if not display_name:
        return "", ""
    parts = display_name.strip().split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return display_name, ""


def is_automated(email_addr: str) -> bool:
    lower = email_addr.lower()
    for kw in SKIP_SENDERS:
        if kw in lower:
            return True
    return False


def domain_from_email(addr: str) -> str:
    parts = addr.split("@")
    if len(parts) == 2:
        return parts[1]
    return ""


def company_from_domain(domain: str) -> str:
    """Ricava nome azienda grezzo dal dominio (rimuove TLD e www)."""
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2].replace("-", " ").replace("_", " ").title()
    return domain


# ── HubSpot ───────────────────────────────────────────────────────────────────
HS_BASE = "https://api.hubapi.com"
HS_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json",
}


def hs_find_contact(email_addr: str) -> Optional[dict]:
    resp = requests.post(
        f"{HS_BASE}/crm/v3/objects/contacts/search",
        headers=HS_HEADERS,
        json={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email_addr}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source_data_1"],
            "limit": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data["total"] > 0:
        return data["results"][0]
    return None


def hs_create_contact(props: dict) -> dict:
    resp = requests.post(
        f"{HS_BASE}/crm/v3/objects/contacts",
        headers=HS_HEADERS,
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, props: dict) -> dict:
    resp = requests.patch(
        f"{HS_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=HS_HEADERS,
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def hs_add_note(contact_id: str, body: str) -> None:
    note = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    }
    resp = requests.post(
        f"{HS_BASE}/crm/v3/objects/notes",
        headers=HS_HEADERS,
        json=note,
        timeout=15,
    )
    if not resp.ok:
        log.warning("Nota non creata per contatto %s: %s", contact_id, resp.text)


# ── Core logic ────────────────────────────────────────────────────────────────
def process_message(msg: dict) -> dict:
    """
    Elabora un messaggio Gmail.
    Ritorna {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "hubspot_id": ...}
    """
    headers = msg.get("payload", {}).get("headers", [])
    sender_email, display_name = parse_sender(headers)

    if not sender_email or is_automated(sender_email):
        return {"status": "Ignorato", "email": sender_email or "(sconosciuto)", "hubspot_id": None}

    domain = domain_from_email(sender_email)
    firstname, lastname = extract_name_parts(display_name)

    existing = hs_find_contact(sender_email)

    if existing:
        contact_id = existing["id"]
        p = existing.get("properties", {})
        updates: dict[str, str] = {}

        if not p.get("firstname") and firstname:
            updates["firstname"] = firstname
        if not p.get("lastname") and lastname:
            updates["lastname"] = lastname
        if not p.get("company") and domain:
            updates["company"] = company_from_domain(domain)
        if not p.get("hs_analytics_source_data_1"):
            updates["hs_analytics_source_data_1"] = "Gmail"

        if updates:
            hs_update_contact(contact_id, updates)
            log.info("AGGIORNATO  %s  (id=%s)  campi=%s", sender_email, contact_id, list(updates))
            return {"status": "Aggiornato", "email": sender_email, "hubspot_id": contact_id}

        return {"status": "Ignorato", "email": sender_email, "hubspot_id": contact_id}

    else:
        props: dict[str, str] = {
            "email": sender_email,
            "hs_analytics_source_data_1": "Gmail",
        }
        if firstname:
            props["firstname"] = firstname
        if lastname:
            props["lastname"] = lastname
        if domain:
            props["company"] = company_from_domain(domain)

        new_contact = hs_create_contact(props)
        contact_id = new_contact["id"]

        hs_add_note(contact_id, "Contatto acquisito da email inbound Gmail. Tag: Inbound Gmail")

        log.info("CREATO      %s  (id=%s)", sender_email, contact_id)
        return {"status": "Creato", "email": sender_email, "hubspot_id": contact_id}


def run_once(service, processed: set[str]) -> tuple[list[dict], set[str]]:
    messages = fetch_new_messages(service, processed)
    results = []
    new_processed = set(processed)

    for msg in messages:
        msg_id = msg["id"]
        result = process_message(msg)
        results.append(result)
        new_processed.add(msg_id)

    return results, new_processed


def run_loop() -> None:
    log.info("Avvio monitoraggio Gmail → HubSpot (polling ogni %ds)", POLL_INTERVAL)
    service = gmail_service()
    processed = load_processed()

    while True:
        try:
            results, processed = run_once(service, processed)
            save_processed(processed)

            creati = sum(1 for r in results if r["status"] == "Creato")
            aggiornati = sum(1 for r in results if r["status"] == "Aggiornato")
            ignorati = sum(1 for r in results if r["status"] == "Ignorato")

            if results:
                log.info(
                    "Ciclo completato — Creati: %d | Aggiornati: %d | Ignorati: %d",
                    creati, aggiornati, ignorati,
                )
                for r in results:
                    if r["status"] != "Ignorato":
                        log.info("  %-12s  %-45s  ID: %s", r["status"], r["email"], r["hubspot_id"])

        except Exception as exc:
            log.error("Errore nel ciclo: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "loop"
    if mode == "once":
        service = gmail_service()
        processed = load_processed()
        results, processed = run_once(service, processed)
        save_processed(processed)
        print("\n{:<12} {:<45} {}".format("Stato", "Email", "HubSpot ID"))
        print("-" * 72)
        for r in results:
            print("{:<12} {:<45} {}".format(r["status"], r["email"], r["hubspot_id"] or "-"))
    else:
        run_loop()
