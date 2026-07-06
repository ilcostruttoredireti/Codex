#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scansiona le email in arrivo su Gmail, estrae i dati del mittente e
sincronizza i contatti in HubSpot (crea o aggiorna, senza duplicati).

Utilizzo:
    python gmail_hubspot_sync.py             # processa le ultime 50 email
    python gmail_hubspot_sync.py --batch 100 # batch personalizzato
    python gmail_hubspot_sync.py --dry-run   # simula senza scrivere in HubSpot
"""

import argparse
import email.utils
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ─── Configurazione ───────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

BASE_DIR = Path(__file__).parent
TOKEN_FILE = BASE_DIR / "token.json"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
STATE_FILE = BASE_DIR / ".sync_state.json"

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"

# Label Gmail applicata ai messaggi già processati (nascosta nella inbox)
PROCESSED_LABEL_NAME = "HubSpot-Synced"

# Domini generici: non vengono usati per inferire il nome azienda
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "proton.me", "fastmail.com",
    "tutanota.com", "gmx.com", "gmx.net", "mail.com", "libero.it",
    "virgilio.it", "tin.it", "alice.it", "tiscali.it",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Gmail ────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """Restituisce un client Gmail autenticato (OAuth2)."""
    creds: Optional[Credentials] = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                log.error(
                    "File credentials.json non trovato. "
                    "Scaricalo da Google Cloud Console → OAuth 2.0."
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_or_create_label(service, name: str) -> str:
    """Restituisce l'ID della label Gmail, creandola se non esiste."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    created = service.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelHide",
            "messageListVisibility": "hide",
        },
    ).execute()
    log.info(f"Label Gmail creata: '{name}'")
    return created["id"]


def fetch_unprocessed_messages(service, max_results: int = 50) -> list[dict]:
    """Recupera messaggi inbox non ancora marcati come processati."""
    query = f"-label:{PROCESSED_LABEL_NAME} in:inbox -from:me"
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    return result.get("messages", [])


def get_message_metadata(service, msg_id: str) -> dict:
    """Estrae From, Subject, Date da un messaggio Gmail."""
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
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    from_raw = headers.get("From", "")
    name, email_addr = email.utils.parseaddr(from_raw)
    email_addr = email_addr.lower().strip()
    domain = email_addr.split("@")[1] if "@" in email_addr else ""

    return {
        "msg_id": msg_id,
        "from_raw": from_raw,
        "email": email_addr,
        "name": name.strip(),
        "domain": domain,
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
    }


def mark_message_processed(service, msg_id: str, label_id: str):
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ─── Parsing dati mittente ────────────────────────────────────────────────────

def split_name(full_name: str) -> tuple[str, str]:
    """'Mario Rossi' → ('Mario', 'Rossi').  'Mario' → ('Mario', '')."""
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) > 1 else (parts[0] if parts else "", "")


def domain_to_company(domain: str) -> str:
    """'acmecorp.com' → 'Acmecorp'. Domini generici → ''."""
    if not domain or domain in GENERIC_DOMAINS:
        return ""
    return domain.split(".")[0].capitalize()


# ─── HubSpot ─────────────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email_addr: str) -> Optional[dict]:
    """Cerca un contatto HubSpot per email. Restituisce il record o None."""
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email_addr}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    resp = requests.post(
        f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> dict:
    resp = requests.post(
        f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, props: dict) -> dict:
    resp = requests.patch(
        f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def hs_add_note(contact_id: str, subject: str, date_str: str, email_addr: str):
    """Aggiunge una nota CRM al contatto come evento timeline."""
    body = (
        f"Email in arrivo da {email_addr}\n"
        f"Oggetto: {subject or '(nessuno)'}\n"
        f"Data: {date_str}\n"
        f"Tag: Inbound Gmail"
    )
    ts_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": ts_ms,
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # nota → contatto
                    }
                ],
            }
        ],
    }
    resp = requests.post(
        f"{HUBSPOT_BASE_URL}/crm/v3/objects/notes",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        log.warning(f"Nota timeline non creata per {email_addr}: {resp.status_code} {resp.text[:200]}")


# ─── Logica di sync ───────────────────────────────────────────────────────────

def sync_sender(sender: dict, dry_run: bool = False) -> dict:
    """
    Elabora un mittente Gmail → HubSpot.

    Restituisce:
        {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "contact_id": ...}
    """
    email_addr = sender["email"]

    if not email_addr or "@" not in email_addr:
        return {"status": "Ignorato", "email": email_addr, "contact_id": None,
                "reason": "Indirizzo email non valido"}

    firstname, lastname = split_name(sender["name"]) if sender["name"] else ("", "")
    company = domain_to_company(sender["domain"])

    existing = hs_find_contact(email_addr)

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})

        # Aggiorna solo i campi mancanti (non sovrascrivere dati esistenti)
        updates: dict[str, str] = {}
        if firstname and not existing_props.get("firstname"):
            updates["firstname"] = firstname
        if lastname and not existing_props.get("lastname"):
            updates["lastname"] = lastname
        if company and not existing_props.get("company"):
            updates["company"] = company

        if updates:
            if not dry_run:
                hs_update_contact(contact_id, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"
    else:
        props: dict[str, str] = {"email": email_addr, "hs_analytics_source": "OTHER"}
        if firstname:
            props["firstname"] = firstname
        if lastname:
            props["lastname"] = lastname
        if company:
            props["company"] = company

        if not dry_run:
            created = hs_create_contact(props)
            contact_id = created["id"]
        else:
            contact_id = "DRY-RUN"
        status = "Creato"

    # Nota timeline
    if not dry_run and status in ("Creato", "Aggiornato"):
        hs_add_note(
            contact_id,
            sender.get("subject", ""),
            sender.get("date", ""),
            email_addr,
        )

    return {"status": status, "email": email_addr, "contact_id": contact_id}


# ─── Stato locale ─────────────────────────────────────────────────────────────

def load_local_state() -> set[str]:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return set(data.get("processed_ids", []))
    return set()


def save_local_state(processed_ids: set[str]):
    STATE_FILE.write_text(
        json.dumps({"processed_ids": sorted(processed_ids)}, indent=2)
    )


# ─── Entry point ──────────────────────────────────────────────────────────────

def run(batch_size: int = 50, dry_run: bool = False) -> list[dict]:
    if not HUBSPOT_API_KEY and not dry_run:
        log.error("HUBSPOT_API_KEY non impostata. Imposta la variabile d'ambiente o usa --dry-run.")
        sys.exit(1)

    log.info(f"Avvio sync{'  [DRY-RUN]' if dry_run else ''} — batch={batch_size}")

    service = get_gmail_service()
    label_id = get_or_create_label(service, PROCESSED_LABEL_NAME)
    local_seen = load_local_state()

    messages = fetch_unprocessed_messages(service, max_results=batch_size)
    log.info(f"Email non processate trovate: {len(messages)}")

    results: list[dict] = []

    for msg_meta in messages:
        msg_id = msg_meta["id"]
        if msg_id in local_seen:
            continue

        try:
            sender = get_message_metadata(service, msg_id)
            log.info(f"  Elaborazione: {sender['from_raw']!r}")

            result = sync_sender(sender, dry_run=dry_run)

            log.info(
                f"    → Stato: {result['status']} | "
                f"Email: {result['email']} | "
                f"ID HubSpot: {result['contact_id']}"
            )
            results.append(result)

            if not dry_run:
                mark_message_processed(service, msg_id, label_id)
            local_seen.add(msg_id)

        except requests.HTTPError as exc:
            log.error(f"  Errore HTTP per messaggio {msg_id}: {exc.response.status_code} {exc.response.text[:300]}")
            results.append({"status": "Errore", "email": "?", "contact_id": None, "reason": str(exc)})
        except HttpError as exc:
            log.error(f"  Errore Gmail API per messaggio {msg_id}: {exc}")
            results.append({"status": "Errore", "email": "?", "contact_id": None, "reason": str(exc)})
        except Exception as exc:
            log.error(f"  Errore imprevisto per messaggio {msg_id}: {exc}", exc_info=True)
            results.append({"status": "Errore", "email": "?", "contact_id": None, "reason": str(exc)})

    save_local_state(local_seen)

    # Riepilogo
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("Creato", "Aggiornato", "Ignorato", "Errore")}
    log.info(
        f"\n{'─'*60}\n"
        f"Sync completato | "
        f"Creati: {counts['Creato']} | "
        f"Aggiornati: {counts['Aggiornato']} | "
        f"Ignorati: {counts['Ignorato']} | "
        f"Errori: {counts['Errore']}\n"
        f"{'─'*60}"
    )

    return results


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--batch", type=int, default=50, help="Numero massimo di email da processare (default: 50)")
    parser.add_argument("--dry-run", action="store_true", help="Simula senza scrivere in HubSpot")
    args = parser.parse_args()
    run(batch_size=args.batch, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
