#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
-----------------------------
Monitora la casella Gmail in arrivo ed esegue l'upsert di ogni mittente
come contatto HubSpot. Evita duplicati usando l'email come chiave univoca.

Utilizzo:
    python gmail_hubspot_sync.py            # esecuzione continua
    python gmail_hubspot_sync.py --once     # singola passata e uscita
    python gmail_hubspot_sync.py --since 7d # processa ultimi N giorni
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
STATE_FILE: str = os.getenv("STATE_FILE", "sync_state.json")
LOG_FILE: str = os.getenv("LOG_FILE", "")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domini email pubblici — non derivare il nome azienda da questi
PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "hotmail.com",
    "hotmail.it", "outlook.com", "live.com", "icloud.com", "me.com",
    "mac.com", "protonmail.com", "proton.me", "tutanota.com", "libero.it",
    "virgilio.it", "tiscali.it", "alice.it", "tin.it", "fastwebnet.it",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if LOG_FILE:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    return logging.getLogger("gmail_hubspot_sync")


log = _setup_logging()


# ---------------------------------------------------------------------------
# Stato persistente
# ---------------------------------------------------------------------------

class SyncState:
    """Traccia l'ID dell'ultimo messaggio Gmail processato."""

    def __init__(self, path: str = STATE_FILE) -> None:
        self._path = Path(path)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_history_id": None, "processed_message_ids": []}

    def save(self) -> None:
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @property
    def last_history_id(self) -> Optional[str]:
        return self._data.get("last_history_id")

    @last_history_id.setter
    def last_history_id(self, value: str) -> None:
        self._data["last_history_id"] = value
        self.save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._data.get("processed_message_ids", [])

    def mark_processed(self, message_id: str) -> None:
        ids: list = self._data.setdefault("processed_message_ids", [])
        if message_id not in ids:
            ids.append(message_id)
            # tieni solo gli ultimi 5000 ID per limitare le dimensioni del file
            if len(ids) > 5000:
                self._data["processed_message_ids"] = ids[-5000:]
        self.save()


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def _gmail_service():
    """Restituisce un servizio Gmail autenticato via OAuth2."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_path = Path(GMAIL_TOKEN_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GMAIL_CREDENTIALS_FILE).exists():
                log.error(
                    "File credenziali Gmail non trovato: %s\n"
                    "Scaricalo da Google Cloud Console → APIs & Services → Credentials",
                    GMAIL_CREDENTIALS_FILE,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


def _extract_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_sender(from_header: str) -> tuple[str, str, str, str]:
    """
    Analizza l'header 'From' e restituisce (display_name, email, firstname, lastname).
    Formato atteso: "Nome Cognome <email@domain.com>" oppure "email@domain.com"
    """
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.strip().lower()

    firstname = lastname = ""
    if display_name:
        parts = display_name.strip().split(None, 1)
        firstname = parts[0] if parts else ""
        lastname = parts[1] if len(parts) > 1 else ""
    else:
        # Prova a ricavare un nome dalla parte locale dell'email
        local = email_addr.split("@")[0] if "@" in email_addr else ""
        # e.g. "mario.rossi" → "Mario", "Rossi"
        name_parts = re.split(r"[._\-+]", local)
        if name_parts:
            firstname = name_parts[0].capitalize()
            if len(name_parts) > 1:
                lastname = name_parts[-1].capitalize()

    return display_name, email_addr, firstname, lastname


def _company_from_domain(email_addr: str) -> str:
    """Ricava il nome azienda dal dominio email (esclusi domini pubblici)."""
    if "@" not in email_addr:
        return ""
    domain = email_addr.split("@", 1)[1].lower()
    if domain in PUBLIC_EMAIL_DOMAINS:
        return ""
    # Rimuovi TLD e capitalizza: "acme.com" → "Acme"
    base = domain.split(".")[0]
    return base.capitalize()


def fetch_new_messages(service, state: SyncState, since_query: str = "") -> list[dict]:
    """
    Restituisce i messaggi Gmail in arrivo non ancora processati.
    `since_query` è una stringa Gmail-style, es. "newer_than:7d".
    """
    query_parts = ["in:inbox", "-from:me"]
    if since_query:
        query_parts.append(since_query)

    query = " ".join(query_parts)
    messages = []
    page_token = None

    while True:
        params: dict = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token

        result = service.users().messages().list(**params).execute()
        batch = result.get("messages", [])
        messages.extend(batch)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    # Filtra già processati
    return [m for m in messages if not state.is_processed(m["id"])]


def get_message_details(service, message_id: str) -> Optional[dict]:
    """Recupera headers rilevanti di un singolo messaggio."""
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = msg.get("payload", {}).get("headers", [])
        return {
            "id": message_id,
            "from": _extract_header(headers, "From"),
            "subject": _extract_header(headers, "Subject"),
            "date": _extract_header(headers, "Date"),
        }
    except Exception as exc:
        log.warning("Errore nel recupero messaggio %s: %s", message_id, exc)
        return None


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------

def _hubspot_client():
    if not HUBSPOT_ACCESS_TOKEN:
        log.error(
            "HUBSPOT_ACCESS_TOKEN non impostato.\n"
            "Aggiungilo nel file .env oppure come variabile d'ambiente."
        )
        sys.exit(1)
    import hubspot
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_hubspot_contact(client, email_addr: str) -> Optional[dict]:
    """Cerca un contatto HubSpot per email. Restituisce il record o None."""
    from hubspot.crm.contacts import ApiException

    try:
        from hubspot.crm.contacts.models import (
            Filter,
            FilterGroup,
            PublicObjectSearchRequest,
        )

        f = Filter(property_name="email", operator="EQ", value=email_addr)
        fg = FilterGroup(filters=[f])
        req = PublicObjectSearchRequest(
            filter_groups=[fg],
            properties=["email", "firstname", "lastname", "company", "leadsource"],
            limit=1,
        )
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if resp.total > 0:
            return resp.results[0]
    except ApiException as exc:
        log.warning("Errore ricerca contatto HubSpot (%s): %s", email_addr, exc)
    return None


def create_hubspot_contact(client, props: dict) -> Optional[str]:
    """Crea un nuovo contatto HubSpot. Restituisce l'ID o None."""
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException

    try:
        resp = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return resp.id
    except ApiException as exc:
        log.warning("Errore creazione contatto HubSpot: %s", exc)
    return None


def update_hubspot_contact(client, contact_id: str, props: dict) -> bool:
    """Aggiorna un contatto esistente con i campi forniti (solo se vuoti)."""
    from hubspot.crm.contacts import SimplePublicObjectInput, ApiException

    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
        return True
    except ApiException as exc:
        log.warning("Errore aggiornamento contatto %s: %s", contact_id, exc)
    return False


def _build_contact_properties(
    firstname: str,
    lastname: str,
    email_addr: str,
    company: str,
) -> dict:
    props: dict = {"email": email_addr, "leadsource": "Gmail"}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def _missing_fields(existing: dict, candidate: dict) -> dict:
    """Restituisce solo i campi di `candidate` assenti o vuoti in `existing`."""
    existing_props = existing.properties if hasattr(existing, "properties") else {}
    updates = {}
    for key, value in candidate.items():
        if key == "email":
            continue  # non aggiornare l'email
        if key == "leadsource":
            # imposta sempre la fonte se non è già "Gmail"
            current = existing_props.get(key, "") or ""
            if "Gmail" not in current:
                updates[key] = value
        else:
            current = existing_props.get(key, "") or ""
            if not current.strip() and value:
                updates[key] = value
    return updates


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------

def process_message(
    message_detail: dict,
    hs_client,
    state: SyncState,
) -> dict:
    """
    Elabora un singolo messaggio Gmail e sincronizza il mittente in HubSpot.
    Restituisce un dizionario con i risultati.
    """
    msg_id = message_detail["id"]
    from_header = message_detail.get("from", "")

    if not from_header:
        state.mark_processed(msg_id)
        return {"status": "Ignorato", "reason": "Header From assente", "email": ""}

    display_name, email_addr, firstname, lastname = _parse_sender(from_header)

    if not email_addr or "@" not in email_addr:
        state.mark_processed(msg_id)
        return {"status": "Ignorato", "reason": "Email non valida", "email": from_header}

    company = _company_from_domain(email_addr)
    candidate_props = _build_contact_properties(firstname, lastname, email_addr, company)

    existing = find_hubspot_contact(hs_client, email_addr)

    if existing is None:
        contact_id = create_hubspot_contact(hs_client, candidate_props)
        status = "Creato" if contact_id else "Errore"
        result_id = contact_id or ""
    else:
        contact_id = existing.id
        updates = _missing_fields(existing, candidate_props)
        if updates:
            update_hubspot_contact(hs_client, contact_id, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"
        result_id = contact_id

    state.mark_processed(msg_id)

    return {
        "status": status,
        "email": email_addr,
        "hubspot_id": result_id,
        "subject": message_detail.get("subject", ""),
        "date": message_detail.get("date", ""),
    }


def run_sync(
    gmail_service,
    hs_client,
    state: SyncState,
    since_query: str = "",
) -> list[dict]:
    """Esegue una singola passata di sincronizzazione. Restituisce i risultati."""
    log.info("Ricerca nuovi messaggi Gmail...")
    messages = fetch_new_messages(gmail_service, state, since_query)
    log.info("Trovati %d messaggi non processati.", len(messages))

    results = []
    for msg in messages:
        detail = get_message_details(gmail_service, msg["id"])
        if not detail:
            state.mark_processed(msg["id"])
            continue

        result = process_message(detail, hs_client, state)
        results.append(result)

        icon = {"Creato": "✚", "Aggiornato": "↺", "Ignorato": "–", "Errore": "✗"}.get(
            result["status"], "?"
        )
        log.info(
            "%s [%s] %s  (HubSpot ID: %s)",
            icon,
            result["status"],
            result.get("email", ""),
            result.get("hubspot_id", "N/A"),
        )

    return results


def print_summary(results: list[dict]) -> None:
    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    ignored = sum(1 for r in results if r["status"] == "Ignorato")
    errors = sum(1 for r in results if r["status"] == "Errore")
    log.info(
        "Riepilogo: %d creati | %d aggiornati | %d ignorati | %d errori",
        created, updated, ignored, errors,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_since(value: str) -> str:
    """Converte '7d', '2h', '30m' in query Gmail come 'newer_than:7d'."""
    if not value:
        return ""
    m = re.fullmatch(r"(\d+)([dhm])", value.strip().lower())
    if not m:
        return ""
    n, unit = m.group(1), m.group(2)
    unit_map = {"d": "d", "h": "h", "m": "m"}
    return f"newer_than:{n}{unit_map[unit]}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincronizza i mittenti Gmail come contatti HubSpot."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui una sola passata e termina (default: ciclo continuo).",
    )
    parser.add_argument(
        "--since",
        default="",
        metavar="DURATION",
        help="Elabora email più recenti di questa durata (es. 7d, 24h, 30m).",
    )
    args = parser.parse_args()

    since_query = _parse_since(args.since)

    log.info("=== Gmail → HubSpot Sync avviato ===")
    if since_query:
        log.info("Filtro temporale: %s", since_query)

    gmail_svc = _gmail_service()
    hs_client = _hubspot_client()
    state = SyncState()

    if args.once:
        results = run_sync(gmail_svc, hs_client, state, since_query)
        print_summary(results)
        return

    # Ciclo continuo
    log.info("Modalità continua attiva — polling ogni %ds. Ctrl+C per fermare.", POLL_INTERVAL)
    try:
        while True:
            try:
                results = run_sync(gmail_svc, hs_client, state, since_query)
                if results:
                    print_summary(results)
                # dopo la prima passata rimuovi il filtro temporale fisso
                since_query = ""
            except Exception as exc:
                log.error("Errore durante la sincronizzazione: %s", exc, exc_info=True)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Sync interrotto dall'utente.")


if __name__ == "__main__":
    main()
