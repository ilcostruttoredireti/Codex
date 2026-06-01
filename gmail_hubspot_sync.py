#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed esegue il sync dei mittenti in HubSpot.

Output per ogni email:
  Stato:       CREATO | AGGIORNATO | IGNORATO | ERRORE
  Email:       indirizzo del mittente
  ID HubSpot:  id contatto HubSpot
"""

import os
import re
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
PROCESSED_LABEL_NAME = os.getenv("GMAIL_PROCESSED_LABEL", "HubSpot-Synced")
HUBSPOT_API = "https://api.hubapi.com"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

# Domini personali comuni — non ricaviamo aziende da questi
_PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "outlook.com", "hotmail.com",
    "hotmail.it", "icloud.com", "protonmail.com", "live.com", "me.com",
    "libero.it", "virgilio.it", "tin.it", "alice.it", "tiscali.it",
}

# Prefissi automatizzati da ignorare (noreply, bounce, ecc.)
_SKIP_PREFIXES = re.compile(
    r"^(noreply|no-reply|do-not-reply|donotreply|notifications?|"
    r"bounce|mailer-daemon|postmaster|info|support|help|admin|"
    r"newsletter|unsubscribe)@",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gmail – autenticazione
# ---------------------------------------------------------------------------
def build_gmail_service():
    creds: Optional[Credentials] = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GMAIL_CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"File credentials OAuth non trovato: {GMAIL_CREDENTIALS_FILE}\n"
                    "Scaricalo da Google Cloud Console → API & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Gmail – label per marcare i messaggi già processati
# ---------------------------------------------------------------------------
def get_or_create_label(service, name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    created = service.users().labels().create(
        userId="me", body={"name": name}
    ).execute()
    log.info("Label Gmail creata: '%s' (id=%s)", name, created["id"])
    return created["id"]


# ---------------------------------------------------------------------------
# Gmail – recupero messaggi non ancora processati
# ---------------------------------------------------------------------------
def fetch_unprocessed(service, processed_label_id: str, max_results: int = 50) -> list:
    """Recupera messaggi inbox non ancora marchiati con il label di sync."""
    query = f"-label:{PROCESSED_LABEL_NAME}"
    resp = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        q=query,
        maxResults=max_results,
    ).execute()
    return resp.get("messages", [])


def get_message_meta(service, msg_id: str) -> dict:
    msg = service.users().messages().get(
        userId="me",
        messageId=msg_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": msg_id,
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "internal_date_ms": int(msg.get("internalDate", "0")),
    }


def mark_as_processed(service, msg_id: str, label_id: str):
    service.users().messages().modify(
        userId="me",
        messageId=msg_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ---------------------------------------------------------------------------
# Parsing del mittente
# ---------------------------------------------------------------------------
def parse_from_header(from_header: str) -> dict:
    """
    Analizza l'header From e restituisce:
      email, first_name, last_name, company
    """
    # "Nome Cognome <email@domain.com>" oppure solo "email@domain.com"
    m = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>', from_header.strip())
    if m:
        display_name = m.group(1).strip()
        email = m.group(2).strip().lower()
    else:
        display_name = ""
        email = from_header.strip().lower()
        # Rimuovi eventuali < >
        email = email.strip("<>")

    parts = display_name.split() if display_name else []
    first_name = parts[0] if parts else ""
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    domain = email.split("@")[-1] if "@" in email else ""
    company = _infer_company(domain)

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
    }


def _infer_company(domain: str) -> str:
    if not domain or domain in _PERSONAL_DOMAINS:
        return ""
    # "acmecorp.it" → "Acmecorp"
    name = domain.split(".")[0]
    return name.capitalize()


def should_skip(email: str) -> bool:
    return bool(_SKIP_PREFIXES.match(email))


# ---------------------------------------------------------------------------
# HubSpot – helpers HTTP
# ---------------------------------------------------------------------------
def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_find_contact_by_email(email: str) -> Optional[dict]:
    """Cerca un contatto per email. Restituisce il record o None."""
    url = f"{HUBSPOT_API}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }],
        "properties": ["email", "firstname", "lastname", "company", "lead_source"],
        "limit": 1,
    }
    resp = requests.post(url, headers=_hs_headers(), json=body, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(sender: dict) -> dict:
    url = f"{HUBSPOT_API}/crm/v3/objects/contacts"
    props: dict = {"email": sender["email"]}
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]
    props["lead_source"] = "Gmail"

    resp = requests.post(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, sender: dict, existing_props: dict) -> dict:
    """Aggiorna solo i campi vuoti nel contatto esistente."""
    props: dict = {}
    if not existing_props.get("firstname") and sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if not existing_props.get("lastname") and sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if not existing_props.get("company") and sender["company"]:
        props["company"] = sender["company"]
    if not existing_props.get("lead_source"):
        props["lead_source"] = "Gmail"

    if not props:
        # Nessun campo da aggiornare
        return {"id": contact_id, "properties": existing_props}

    url = f"{HUBSPOT_API}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def hs_add_inbound_email_activity(
    contact_id: str,
    subject: str,
    sender_email: str,
    timestamp_ms: int,
):
    """Crea un'attività email in ingresso nella timeline del contatto."""
    url = f"{HUBSPOT_API}/crm/v3/objects/emails"
    body = {
        "properties": {
            "hs_email_direction": "INCOMING_EMAIL",
            "hs_email_subject": subject or "(senza oggetto)",
            "hs_email_sender_email": sender_email,
            "hs_timestamp": str(timestamp_ms),
            "hs_email_status": "RECEIVED",
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": 198,  # CONTACT_TO_EMAIL
            }],
        }],
    }
    resp = requests.post(url, headers=_hs_headers(), json=body, timeout=15)
    if resp.status_code not in (200, 201):
        log.debug("Attività email non creata (non critico): %s", resp.text[:200])


def hs_add_tag_note(contact_id: str, tag: str = "Inbound Gmail"):
    """Aggiunge una nota con il tag specificato alla timeline del contatto."""
    url = f"{HUBSPOT_API}/crm/v3/objects/notes"
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = {
        "properties": {
            "hs_note_body": f"Tag: {tag} — Contatto sincronizzato da email Gmail in ingresso.",
            "hs_timestamp": str(ts),
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": 202,  # CONTACT_TO_NOTE
            }],
        }],
    }
    resp = requests.post(url, headers=_hs_headers(), json=body, timeout=15)
    if resp.status_code not in (200, 201):
        log.debug("Nota tag non creata (non critico): %s", resp.text[:200])


# ---------------------------------------------------------------------------
# Elaborazione di un singolo messaggio
# ---------------------------------------------------------------------------
def process_message(service, msg: dict, processed_label_id: str) -> dict:
    meta = get_message_meta(service, msg["id"])
    sender = parse_from_header(meta["from"])

    result = {
        "status": "IGNORATO",
        "email": sender["email"],
        "contact_id": None,
    }

    # Validazione email
    if not sender["email"] or "@" not in sender["email"]:
        mark_as_processed(service, msg["id"], processed_label_id)
        return result

    # Ignora indirizzi automatizzati
    if should_skip(sender["email"]):
        mark_as_processed(service, msg["id"], processed_label_id)
        return result

    try:
        existing = hs_find_contact_by_email(sender["email"])

        if existing:
            contact_id = existing["id"]
            existing_props = existing.get("properties", {})
            hs_update_contact(contact_id, sender, existing_props)
            result["status"] = "AGGIORNATO"
        else:
            created = hs_create_contact(sender)
            contact_id = created["id"]
            hs_add_tag_note(contact_id, "Inbound Gmail")
            result["status"] = "CREATO"

        result["contact_id"] = contact_id

        # Timeline: attività email ricevuta
        hs_add_inbound_email_activity(
            contact_id,
            meta["subject"],
            sender["email"],
            meta["internal_date_ms"],
        )

    except requests.HTTPError as exc:
        log.error(
            "Errore HubSpot per '%s': %s %s",
            sender["email"],
            exc.response.status_code,
            exc.response.text[:300],
        )
        result["status"] = "ERRORE"

    mark_as_processed(service, msg["id"], processed_label_id)
    return result


# ---------------------------------------------------------------------------
# Ciclo di sync
# ---------------------------------------------------------------------------
STATUS_ICON = {
    "CREATO": "✚",
    "AGGIORNATO": "↻",
    "IGNORATO": "—",
    "ERRORE": "✗",
}


def run_sync_cycle(service, processed_label_id: str):
    messages = fetch_unprocessed(service, processed_label_id)
    if not messages:
        log.info("Nessun nuovo messaggio.")
        return

    log.info("Trovati %d messaggi da elaborare.", len(messages))
    for msg in messages:
        result = process_message(service, msg, processed_label_id)
        icon = STATUS_ICON.get(result["status"], "?")
        log.info(
            "%s  Stato: %-10s | Email: %-40s | ID HubSpot: %s",
            icon,
            result["status"],
            result["email"] or "—",
            result["contact_id"] or "—",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    if not HUBSPOT_TOKEN:
        raise SystemExit(
            "ERRORE: variabile HUBSPOT_ACCESS_TOKEN non impostata.\n"
            "Copiare .env.example in .env e inserire il token."
        )

    log.info("=" * 60)
    log.info("  Gmail → HubSpot Contact Sync  (intervallo: %ds)", POLL_INTERVAL)
    log.info("=" * 60)

    service = build_gmail_service()
    label_id = get_or_create_label(service, PROCESSED_LABEL_NAME)
    log.info("Label di tracciamento: '%s' (id=%s)", PROCESSED_LABEL_NAME, label_id)

    while True:
        try:
            run_sync_cycle(service, label_id)
        except Exception:
            log.exception("Errore imprevisto nel ciclo di sync")
        log.info("Prossimo controllo in %d secondi…\n", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
