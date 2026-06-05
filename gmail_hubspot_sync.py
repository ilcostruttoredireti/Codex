#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=====================================================================
Monitora la casella Gmail in entrata ed esegue automaticamente la
sincronizzazione dei mittenti come contatti HubSpot.

Comportamento per ogni email ricevuta:
  - Estrae email, nome, cognome e dominio aziendale del mittente
  - Cerca il contatto in HubSpot per email (chiave univoca)
  - Se esiste  → aggiorna i campi mancanti + aggiunge nota
  - Se non esiste → crea nuovo contatto con fonte "Gmail"
  - Aggiunge un'attività di timeline (nota) con tag "Inbound Gmail"
  - Salta indirizzi automatici/noreply

Utilizzo:
    python gmail_hubspot_sync.py               # elaborazione singola
    python gmail_hubspot_sync.py --watch        # monitoraggio continuo
    python gmail_hubspot_sync.py --watch --interval 120   # ogni 2 minuti
    python gmail_hubspot_sync.py --max-emails 200         # controlla più email
"""

import os
import sys
import json
import time
import logging
import argparse
import re
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Optional, Tuple

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()


# ─── Costanti ─────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDENTIALS_FILE = "credentials.json"

STATE_FILE = ".sync_state.json"
HUBSPOT_BASE = "https://api.hubapi.com"
MAX_STORED_IDS = 50_000   # bound the state file size

# Parti locali dell'email (prima della @) che indicano mittenti automatici
SKIP_LOCAL_PARTS: frozenset = frozenset({
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "do_not_reply", "notifications", "notification", "mailer-daemon",
    "postmaster", "bounce", "bounces", "autoresponder", "automated",
    "unsubscribe", "newsletter", "news", "alert", "alerts", "updates",
    "system", "robot", "bot",
})

# Domini/pattern che indicano piattaforme bulk o transazionali
_SKIP_DOMAIN_PATTERN = re.compile(
    r"(sendgrid\.net|mailchimp\.com|constantcontact\.com|amazonses\.com|"
    r"mandrillapp\.com|mailgun\.org|sparkpostmail\.com|sendinblue\.com|"
    r"postmarkapp\.com|klaviyo\.com|hubspotemail\.net|"
    r"\.(bounce|mail)\.)$",
    re.IGNORECASE,
)

# TLD comuni da rimuovere per ricavare il nome azienda
_COMMON_TLDS: frozenset = frozenset({
    "com", "net", "org", "io", "co", "uk", "de", "fr", "it", "es",
    "nl", "be", "ch", "at", "au", "ca", "br", "mx", "in", "jp", "eu",
    "biz", "info", "online", "tech", "app", "dev",
})


# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Stato persistente ────────────────────────────────────────────────────────

def load_state() -> dict[str, Any]:
    """Carica lo stato di sincronizzazione dal file locale."""
    try:
        return json.loads(Path(STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"processed_ids": [], "last_run": None}


def save_state(state: dict[str, Any]) -> None:
    """Salva lo stato mantenendo al massimo MAX_STORED_IDS id."""
    state["processed_ids"] = state["processed_ids"][-MAX_STORED_IDS:]
    Path(STATE_FILE).write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ─── Autenticazione Gmail ─────────────────────────────────────────────────────

def get_gmail_service():
    """Restituisce un client Gmail autenticato via OAuth2."""
    creds: Optional[Credentials] = None

    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GMAIL_CREDENTIALS_FILE).exists():
                log.error(
                    "File '%s' non trovato. Scaricalo da Google Cloud Console "
                    "(OAuth 2.0 Client ID → Desktop App) e posizionalo nella "
                    "directory corrente.", GMAIL_CREDENTIALS_FILE
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ─── Parsing mittente ─────────────────────────────────────────────────────────

def is_skippable(email_addr: str) -> bool:
    """Restituisce True per indirizzi automatici che non vanno sincronizzati."""
    email_addr = email_addr.lower().strip()
    local, _, domain = email_addr.partition("@")
    if not domain:
        return True

    # Controlla parte locale
    local_clean = re.sub(r"[\.\-_\+]", "", local)
    for skip in SKIP_LOCAL_PARTS:
        skip_clean = re.sub(r"[\.\-_]", "", skip)
        if local == skip or local_clean == skip_clean or local.startswith(skip):
            return True

    # Controlla dominio
    if _SKIP_DOMAIN_PATTERN.search(domain):
        return True

    return False


def parse_sender(from_header: str) -> tuple[str, str, str, str]:
    """
    Analizza l'header From e restituisce
    (display_name, first_name, last_name, email).
    """
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()

    first_name = last_name = ""
    if display_name:
        parts = display_name.strip().split()
        first_name = parts[0] if parts else ""
        last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    return display_name, first_name, last_name, email_addr


def company_from_domain(domain: str) -> str:
    """
    Ricava il nome dell'azienda dal dominio email.
    Esempi: acme.com → "Acme", john.doe.co.uk → "John Doe"
    """
    parts = domain.lower().split(".")
    name_parts = [p for p in parts if p not in _COMMON_TLDS]
    if not name_parts:
        name_parts = parts[:1]
    return " ".join(p.capitalize() for p in name_parts)


# ─── Gmail: recupero messaggi ─────────────────────────────────────────────────

def fetch_inbox_message_ids(service, max_results: int = 100) -> list[str]:
    """Restituisce gli ID dei messaggi nella inbox (escluso spam/trash)."""
    try:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q="in:inbox -in:sent", maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in resp.get("messages", [])]
    except HttpError as exc:
        log.error("Errore lista Gmail: %s", exc)
        return []


def get_message_metadata(
    service, message_id: str
) -> tuple[str, str, str]:
    """
    Recupera From, Subject e Date di un messaggio.
    Restituisce (from_header, subject, date).
    """
    try:
        msg = (
            service.users()
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
        return (
            headers.get("From", ""),
            headers.get("Subject", "(nessun oggetto)"),
            headers.get("Date", ""),
        )
    except HttpError as exc:
        log.error("Errore recupero messaggio %s: %s", message_id, exc)
        return "", "", ""


# ─── HubSpot API ──────────────────────────────────────────────────────────────

def _hs_headers() -> dict[str, str]:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()
    if not token:
        log.error(
            "Variabile d'ambiente HUBSPOT_ACCESS_TOKEN non impostata. "
            "Crea una Private App in HubSpot Settings → Integrations → Private Apps."
        )
        sys.exit(1)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def hs_search_contact(email: str) -> Optional[dict]:
    """Cerca un contatto HubSpot per email. Restituisce il record o None."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": [
            "email", "firstname", "lastname", "company",
            "hs_lead_source", "lifecyclestage",
        ],
        "limit": 1,
    }
    r = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict[str, str]) -> dict:
    """Crea un nuovo contatto HubSpot."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    r = requests.post(
        url, json={"properties": props}, headers=_hs_headers(), timeout=15
    )
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, props: dict[str, str]) -> dict:
    """Aggiorna un contatto HubSpot esistente."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    r = requests.patch(
        url, json={"properties": props}, headers=_hs_headers(), timeout=15
    )
    r.raise_for_status()
    return r.json()


def hs_create_note(contact_id: str, body: str) -> Optional[dict]:
    """
    Crea una nota HubSpot associata al contatto
    (attività nella timeline del contatto).
    """
    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": str(int(time.time() * 1000)),
        },
        "associations": [
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
    }
    try:
        r = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as exc:
        log.warning("Impossibile creare nota per contatto %s: %s", contact_id, exc)
        return None


# ─── Logica principale per singolo messaggio ──────────────────────────────────

def process_message(
    service,
    message_id: str,
    processed_ids: set[str],
) -> dict[str, str]:
    """
    Elabora un singolo messaggio Gmail.

    Restituisce un dizionario con:
      status      → "Creato" | "Aggiornato" | "Ignorato" | "Errore: ..."
      email       → indirizzo del mittente
      contact_id  → ID HubSpot (vuoto se ignorato/errore)
      message_id  → ID del messaggio Gmail
    """
    result: dict[str, str] = {
        "message_id": message_id,
        "email": "",
        "contact_id": "",
        "status": "Ignorato",
    }

    if message_id in processed_ids:
        result["status"] = "Ignorato (già elaborato)"
        return result

    from_header, subject, date = get_message_metadata(service, message_id)
    if not from_header:
        result["status"] = "Ignorato (nessun header From)"
        return result

    display_name, first_name, last_name, email_addr = parse_sender(from_header)
    result["email"] = email_addr

    if not email_addr or "@" not in email_addr:
        result["status"] = "Ignorato (email non valida)"
        return result

    if is_skippable(email_addr):
        result["status"] = "Ignorato (automatico/noreply)"
        return result

    domain = email_addr.split("@")[1]
    company = company_from_domain(domain)

    # ── Verifica su HubSpot ──────────────────────────────────────────────────
    try:
        existing = hs_search_contact(email_addr)
    except requests.HTTPError as exc:
        result["status"] = f"Errore (ricerca HubSpot: {exc})"
        log.error("HubSpot search per %s: %s", email_addr, exc)
        return result

    note_body = (
        f"📧 Email ricevuta via Gmail\n"
        f"Da: {display_name} <{email_addr}>\n"
        f"Oggetto: {subject}\n"
        f"Data: {date}\n"
        f"Tag: Inbound Gmail"
    )

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})

        # Aggiorna solo campi vuoti
        updates: dict[str, str] = {}
        if first_name and not existing_props.get("firstname"):
            updates["firstname"] = first_name
        if last_name and not existing_props.get("lastname"):
            updates["lastname"] = last_name
        if company and not existing_props.get("company"):
            updates["company"] = company

        try:
            if updates:
                hs_update_contact(contact_id, updates)
        except requests.HTTPError as exc:
            result["status"] = f"Errore (aggiornamento HubSpot: {exc})"
            log.error("HubSpot update contatto %s: %s", contact_id, exc)
            return result

        hs_create_note(contact_id, note_body)

        result["contact_id"] = contact_id
        result["status"] = "Aggiornato"
    else:
        # Crea nuovo contatto
        props: dict[str, str] = {
            "email": email_addr,
            "hs_lead_source": "Gmail",
            "lifecyclestage": "lead",
        }
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if company:
            props["company"] = company

        try:
            new_contact = hs_create_contact(props)
        except requests.HTTPError as exc:
            # Gestione graceful del conflitto email (409)
            if exc.response is not None and exc.response.status_code == 409:
                log.warning(
                    "Contatto %s già esistente (conflitto 409), riprovo come update.",
                    email_addr,
                )
                try:
                    existing_retry = hs_search_contact(email_addr)
                    if existing_retry:
                        contact_id = existing_retry["id"]
                        hs_create_note(contact_id, note_body)
                        result["contact_id"] = contact_id
                        result["status"] = "Aggiornato"
                        return result
                except Exception:
                    pass
            result["status"] = f"Errore (creazione HubSpot: {exc})"
            log.error("HubSpot create per %s: %s", email_addr, exc)
            return result

        contact_id = new_contact["id"]
        hs_create_note(contact_id, note_body)

        result["contact_id"] = contact_id
        result["status"] = "Creato"

    return result


# ─── Ciclo di sincronizzazione ────────────────────────────────────────────────

def sync_once(service, max_emails: int = 100) -> list[dict[str, str]]:
    """
    Elabora tutti i messaggi inbox non ancora processati.
    Restituisce la lista dei risultati.
    """
    state = load_state()
    processed_ids: set[str] = set(state.get("processed_ids", []))

    message_ids = fetch_inbox_message_ids(service, max_results=max_emails)
    if not message_ids:
        log.info("Nessun messaggio trovato nella inbox.")
        return []

    new_ids = [mid for mid in message_ids if mid not in processed_ids]
    if not new_ids:
        log.info("Nessun nuovo messaggio da elaborare (%d già visti).", len(message_ids))
        return []

    log.info("Elaborazione di %d nuovi messaggi...", len(new_ids))

    results: list[dict[str, str]] = []
    newly_processed: list[str] = []

    for msg_id in new_ids:
        result = process_message(service, msg_id, processed_ids)
        results.append(result)
        newly_processed.append(msg_id)

        icon = {
            "Creato": "✅",
            "Aggiornato": "🔄",
        }.get(result["status"], "⏭️" if result["status"].startswith("Ignorato") else "❌")

        log.info(
            "%s  %-30s  status=%-35s  hs_id=%s",
            icon,
            result.get("email", "-"),
            result["status"],
            result.get("contact_id", "-"),
        )

        # Piccola pausa per rispettare i rate-limit di HubSpot (100 req/10s)
        time.sleep(0.12)

    # Aggiorna stato
    state["processed_ids"] = list(processed_ids) + newly_processed
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return results


def print_summary(results: list[dict[str, str]]) -> None:
    """Stampa il riepilogo a fine elaborazione."""
    if not results:
        print("\n── Nessuna nuova email da elaborare ──")
        return

    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    skipped = sum(1 for r in results if r["status"].startswith("Ignorato"))
    errors = sum(1 for r in results if r["status"].startswith("Errore"))

    print(f"\n{'═' * 65}")
    print(f"  RIEPILOGO SINCRONIZZAZIONE  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'─' * 65}")
    print(f"  Email elaborate : {len(results)}")
    print(f"  ✅ Creati       : {created}")
    print(f"  🔄 Aggiornati   : {updated}")
    print(f"  ⏭️  Ignorati    : {skipped}")
    print(f"  ❌ Errori       : {errors}")
    print(f"{'═' * 65}")

    synced = [r for r in results if r["status"] in ("Creato", "Aggiornato")]
    if synced:
        print(f"\n  {'STATUS':<12} {'EMAIL':<38} {'HUBSPOT ID'}")
        print(f"  {'─' * 63}")
        for r in synced:
            print(f"  {r['status']:<12} {r['email']:<38} {r['contact_id']}")
        print()


# ─── Entrypoint ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincronizza i mittenti Gmail come contatti HubSpot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Monitoraggio continuo: elabora nuove email periodicamente.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        metavar="SECONDI",
        help="Intervallo di polling in secondi con --watch (default: 60).",
    )
    parser.add_argument(
        "--max-emails",
        type=int,
        default=100,
        metavar="N",
        help="Numero massimo di email da controllare per ciclo (default: 100).",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Resetta lo stato interno (rielabora tutte le email presenti).",
    )
    args = parser.parse_args()

    if args.reset_state:
        Path(STATE_FILE).unlink(missing_ok=True)
        log.info("Stato resettato.")

    log.info("Autenticazione Gmail in corso...")
    service = get_gmail_service()
    log.info("Gmail pronto.")

    if args.watch:
        log.info(
            "Modalità watch attiva — polling ogni %ds. Ctrl+C per fermare.",
            args.interval,
        )
        while True:
            try:
                results = sync_once(service, max_emails=args.max_emails)
                print_summary(results)
                time.sleep(args.interval)
            except KeyboardInterrupt:
                log.info("Fermato dall'utente.")
                break
            except Exception as exc:
                log.error("Errore durante la sincronizzazione: %s", exc, exc_info=True)
                time.sleep(args.interval)
    else:
        results = sync_once(service, max_emails=args.max_emails)
        print_summary(results)


if __name__ == "__main__":
    main()
