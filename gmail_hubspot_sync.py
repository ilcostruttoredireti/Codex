#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora Gmail, estrae mittenti e li sincronizza in HubSpot.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configurazione ────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
GMAIL_TOKEN_FILE = Path(os.environ.get("GMAIL_TOKEN_FILE", "token.json"))
GMAIL_CREDENTIALS_FILE = Path(os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json"))

HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
HUBSPOT_BASE = "https://api.hubapi.com"

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL_SECONDS", "0"))  # 0 = una volta sola

# Domini di email gratuiti da non usare come azienda
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "hotmail.com",
    "hotmail.it", "outlook.com", "outlook.it", "live.com", "live.it",
    "icloud.com", "me.com", "mac.com", "aol.com", "mail.com", "email.it",
    "libero.it", "alice.it", "tin.it", "tiscali.it", "virgilio.it",
    "fastwebnet.it", "protonmail.com", "pm.me", "tutanota.com",
    "tutanota.de", "gmx.com", "gmx.de", "web.de", "inbox.com",
}

# Mittenti da ignorare automaticamente
IGNORE_PATTERNS = re.compile(
    r"(noreply|no-reply|donotreply|do-not-reply|notifications?@|"
    r"mailer-daemon|postmaster|bounce|alert@|support@.*automated|"
    r"newsletter@|unsubscribe@)",
    re.IGNORECASE,
)

# Etichetta Gmail per i messaggi processati (evita riprocessing)
GMAIL_LABEL_NAME = "hubspot-synced"


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """Autentica e restituisce il servizio Gmail API."""
    creds = None

    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GMAIL_CREDENTIALS_FILE.exists():
                # Prova a caricare da variabile d'ambiente (per CI)
                creds_json = os.environ.get("GMAIL_CREDENTIALS_JSON")
                token_json = os.environ.get("GMAIL_TOKEN_JSON")
                if creds_json and token_json:
                    GMAIL_CREDENTIALS_FILE.write_text(creds_json)
                    GMAIL_TOKEN_FILE.write_text(token_json)
                    creds = Credentials.from_authorized_user_file(
                        str(GMAIL_TOKEN_FILE), GMAIL_SCOPES
                    )
                else:
                    raise FileNotFoundError(
                        f"File credenziali Gmail non trovato: {GMAIL_CREDENTIALS_FILE}\n"
                        "Scarica credentials.json da Google Cloud Console oppure imposta "
                        "le variabili GMAIL_CREDENTIALS_JSON e GMAIL_TOKEN_JSON."
                    )
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)

        GMAIL_TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_or_create_label(service, name: str) -> str:
    """Restituisce l'ID dell'etichetta, creandola se non esiste."""
    result = service.users().labels().list(userId="me").execute()
    for label in result.get("labels", []):
        if label["name"] == name:
            return label["id"]

    new_label = service.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelHide",
            "messageListVisibility": "hide",
        },
    ).execute()
    log.info("Etichetta Gmail creata: %s (ID: %s)", name, new_label["id"])
    return new_label["id"]


def fetch_unprocessed_messages(service, label_id: str) -> list[dict]:
    """Recupera messaggi inbox non ancora etichettati come processati."""
    query = f"in:inbox -label:{GMAIL_LABEL_NAME} -from:me"
    messages = []
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token

        result = service.users().messages().list(**kwargs).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return messages


def get_message_meta(service, msg_id: str) -> dict | None:
    """Recupera i metadati rilevanti di un messaggio."""
    try:
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
    except HttpError as e:
        log.warning("Impossibile leggere messaggio %s: %s", msg_id, e)
        return None

    headers = {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }

    return {
        "id": msg_id,
        "from": headers.get("from", ""),
        "subject": headers.get("subject", "(nessun oggetto)"),
        "date": headers.get("date", ""),
        "internal_date": int(msg.get("internalDate", 0)) // 1000,
    }


def mark_as_processed(service, msg_id: str, label_id: str):
    """Applica l'etichetta 'hubspot-synced' al messaggio."""
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ── Parsing mittente ──────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> dict | None:
    """Estrae email, nome, cognome, dominio e azienda dall'intestazione From."""
    display_name, email = parseaddr(from_header)
    if not email or "@" not in email:
        return None

    email = email.lower().strip()
    domain = email.split("@")[1]

    # Ignora mittenti automatici
    if IGNORE_PATTERNS.search(email):
        return None

    # Nome / Cognome
    first_name, last_name = "", ""
    name_clean = display_name.strip().strip('"').strip("'")
    if name_clean:
        parts = name_clean.split()
        if len(parts) >= 2:
            first_name = parts[0].capitalize()
            last_name = " ".join(parts[1:]).title()
        else:
            first_name = parts[0].capitalize()

    # Azienda dal dominio (escludi provider gratuiti)
    company = ""
    if domain not in FREE_EMAIL_DOMAINS:
        raw = domain.split(".")[0]
        company = raw.replace("-", " ").replace("_", " ").title()

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
        "display_name": display_name,
    }


# ── HubSpot ───────────────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("HUBSPOT_API_KEY non impostato.")
        self._s = requests.Session()
        self._s.headers.update(
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )

    def _get(self, path: str, **params) -> dict:
        r = self._s.get(f"{HUBSPOT_BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = self._s.post(f"{HUBSPOT_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, body: dict) -> dict:
        r = self._s.patch(f"{HUBSPOT_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

    def find_contact(self, email: str) -> dict | None:
        """Cerca contatto per email. Ritorna il primo risultato o None."""
        data = self._post(
            "/crm/v3/objects/contacts/search",
            {
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": [
                    "email", "firstname", "lastname", "company",
                    "hs_lead_source", "website",
                ],
                "limit": 1,
            },
        )
        results = data.get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        return self._post("/crm/v3/objects/contacts", {"properties": props})

    def update_contact(self, contact_id: str, props: dict) -> dict:
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})

    def add_email_activity(
        self, contact_id: str, from_email: str, subject: str, received_at: int
    ):
        """Registra l'email ricevuta come attività nella timeline del contatto."""
        try:
            self._post(
                "/engagements/v1/engagements",
                {
                    "engagement": {
                        "active": True,
                        "type": "EMAIL",
                        "timestamp": received_at * 1000,
                    },
                    "associations": {"contactIds": [int(contact_id)]},
                    "metadata": {
                        "from": {"email": from_email},
                        "subject": subject,
                        "text": (
                            f"Email inbound ricevuta da {from_email} via Gmail.\n"
                            f"Tag: Inbound Gmail"
                        ),
                        "direction": "INCOMING",
                    },
                },
            )
        except requests.HTTPError as e:
            log.debug("Attività timeline non registrata: %s", e)


def build_hs_props(sender: dict, existing: dict | None) -> dict:
    """
    Costruisce il dizionario proprietà HubSpot.
    Non sovrascrive campi già valorizzati nel contatto esistente.
    """
    existing_props = (existing or {}).get("properties", {})
    props: dict[str, str] = {"email": sender["email"], "hs_lead_source": "Gmail"}

    if sender["first_name"] and not existing_props.get("firstname"):
        props["firstname"] = sender["first_name"]
    if sender["last_name"] and not existing_props.get("lastname"):
        props["lastname"] = sender["last_name"]
    if sender["company"] and not existing_props.get("company"):
        props["company"] = sender["company"]
    if sender["domain"] and not existing_props.get("website"):
        props["website"] = f"https://{sender['domain']}"

    return props


# ── Sync ──────────────────────────────────────────────────────────────────────

def sync_once(gmail, hs: HubSpotClient, label_id: str) -> list[dict]:
    """Esegue un ciclo di sincronizzazione. Ritorna la lista dei risultati."""
    messages = fetch_unprocessed_messages(gmail, label_id)
    log.info("Messaggi non processati trovati: %d", len(messages))

    results = []

    for msg_stub in messages:
        meta = get_message_meta(gmail, msg_stub["id"])
        if not meta:
            continue

        sender = parse_sender(meta["from"])

        if not sender:
            # Mittente automatico o non parsabile → etichetta e ignora
            mark_as_processed(gmail, meta["id"], label_id)
            log.debug("Ignorato: %s", meta["from"])
            results.append(
                {"status": "Ignorato", "email": meta["from"], "contact_id": None}
            )
            continue

        email = sender["email"]
        log.info("Processo: %s", email)

        try:
            existing = hs.find_contact(email)
            props = build_hs_props(sender, existing)

            if existing:
                contact_id = existing["id"]
                # Aggiorna solo se ci sono campi nuovi da compilare
                updatable = {k: v for k, v in props.items() if k not in ("email",)}
                existing_filled = {
                    k for k, v in existing.get("properties", {}).items() if v
                }
                new_fields = {k: v for k, v in updatable.items() if k not in existing_filled}

                if new_fields:
                    hs.update_contact(contact_id, new_fields)
                    status = "Aggiornato"
                else:
                    status = "Ignorato"  # contatto completo, nulla da aggiornare
            else:
                created = hs.create_contact(props)
                contact_id = created["id"]
                status = "Creato"

            # Registra attività timeline
            hs.add_email_activity(
                contact_id, email, meta["subject"], meta["internal_date"]
            )

            mark_as_processed(gmail, meta["id"], label_id)
            results.append(
                {"status": status, "email": email, "contact_id": contact_id}
            )
            log.info("%-10s | %-40s | ID: %s", status, email, contact_id)

        except requests.HTTPError as exc:
            log.error("Errore HubSpot per %s: %s — %s", email, exc, exc.response.text)
            results.append({"status": "Errore", "email": email, "contact_id": None})

    return results


def print_report(results: list[dict]):
    """Stampa il riepilogo finale."""
    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    ignored = sum(1 for r in results if r["status"] == "Ignorato")
    errors = sum(1 for r in results if r["status"] == "Errore")

    print("\n" + "=" * 65)
    print(f"  RIEPILOGO SINCRONIZZAZIONE  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)
    print(f"  Creati   : {created}")
    print(f"  Aggiornati: {updated}")
    print(f"  Ignorati  : {ignored}")
    print(f"  Errori    : {errors}")
    print("-" * 65)
    for r in results:
        cid = str(r["contact_id"]) if r["contact_id"] else "—"
        print(f"  {r['status']:<10} | {r['email']:<40} | {cid}")
    print("=" * 65 + "\n")


def main():
    if not HUBSPOT_API_KEY:
        log.error("Variabile HUBSPOT_API_KEY non impostata. Uscita.")
        sys.exit(1)

    gmail = get_gmail_service()
    hs = HubSpotClient(HUBSPOT_API_KEY)
    label_id = get_or_create_label(gmail, GMAIL_LABEL_NAME)

    if SYNC_INTERVAL > 0:
        log.info("Modalità continua: ciclo ogni %ds", SYNC_INTERVAL)
        while True:
            try:
                results = sync_once(gmail, hs, label_id)
                print_report(results)
            except Exception as exc:
                log.exception("Errore nel ciclo di sync: %s", exc)
            time.sleep(SYNC_INTERVAL)
    else:
        results = sync_once(gmail, hs, label_id)
        print_report(results)


if __name__ == "__main__":
    main()
