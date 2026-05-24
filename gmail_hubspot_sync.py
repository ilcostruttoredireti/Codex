#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
================================
Monitora le email in arrivo su Gmail, estrae i dati dei mittenti
e li sincronizza automaticamente in HubSpot evitando duplicati.

Autore: Sistema automatizzato Claude
Data:   2026-05-24
"""

import re
import time
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────
# Dipendenze esterne (pip install google-auth-oauthlib
#                              google-api-python-client
#                              hubspot-api-client)
# ─────────────────────────────────────────────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    import hubspot
    from hubspot.crm.contacts import (
        SimplePublicObjectInputForCreate,
        ApiException as HsApiException,
    )
    from hubspot.crm.contacts.models import SimplePublicObjectInput
except ImportError as e:
    print(f"[ERRORE] Dipendenza mancante: {e}")
    print("Installa con: pip install google-auth-oauthlib google-api-python-client hubspot-api-client")
    raise

# ─────────────────────────────────────────────────────────────
# Configurazione logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Costanti
# ─────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE   = "token.json"
CREDS_FILE   = "credentials.json"

# Indirizzi da ignorare (sistemi automatici, newsletter, ecc.)
IGNORED_PATTERNS = re.compile(
    r"(noreply|no-reply|notifications?|updates?|newsletter|mailer-daemon|"
    r"postmaster|bounce|autorespond|donotreply|do-not-reply|"
    r"info@|support@|help@|admin@)",
    re.IGNORECASE,
)

# Domini da ignorare completamente
IGNORED_DOMAINS = {
    "accounts.google.com", "google.com", "googlemail.com",
    "linkedin.com", "facebook.com", "twitter.com",
    "announce.fiverr.com", "fiverr.com",
    "treatwell.it",
}

SOURCE_LABEL  = "Gmail"
INBOUND_TAG   = "Inbound Gmail"
POLL_INTERVAL = 60  # secondi tra un controllo e l'altro

# ─────────────────────────────────────────────────────────────
# Strutture dati
# ─────────────────────────────────────────────────────────────
@dataclass
class ContactInfo:
    email:      str
    first_name: str         = ""
    last_name:  str         = ""
    company:    str         = ""
    domain:     str         = ""
    subject:    str         = ""
    thread_id:  str         = ""
    received:   str         = ""


@dataclass
class SyncResult:
    email:      str
    status:     str           # "CREATO" | "AGGIORNATO" | "IGNORATO"
    reason:     str           = ""
    hubspot_id: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# Autenticazione Gmail
# ─────────────────────────────────────────────────────────────
def get_gmail_service():
    """Autentica con Gmail OAuth2 e restituisce il client."""
    creds = None
    import os
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ─────────────────────────────────────────────────────────────
# Parsing email
# ─────────────────────────────────────────────────────────────
def extract_sender(from_header: str) -> tuple[str, str, str]:
    """
    Estrae (nome, email, dominio) dall'header From:.
    Gestisce formati: "Nome Cognome <email>" oppure solo "email".
    """
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>$', from_header.strip())
    if match:
        name  = match.group(1).strip()
        email = match.group(2).strip().lower()
    else:
        name  = ""
        email = from_header.strip().lower()

    domain = email.split("@")[-1] if "@" in email else ""
    return name, email, domain


def parse_name(full_name: str) -> tuple[str, str]:
    """Divide un nome completo in (nome, cognome)."""
    parts = full_name.strip().split()
    if len(parts) == 0:
        return "", ""
    elif len(parts) == 1:
        return parts[0], ""
    else:
        return parts[0], " ".join(parts[1:])


def company_from_domain(domain: str) -> str:
    """
    Tenta di ricavare il nome azienda dal dominio email.
    Es. "latestata.it" → "La Testata"
    """
    if not domain or domain in IGNORED_DOMAINS:
        return ""
    # Rimuove estensione (.it, .com, ecc.)
    base = domain.split(".")[0]
    # Capitalizza e sostituisce trattini/underscore
    return base.replace("-", " ").replace("_", " ").title()


def is_automated(email: str, domain: str) -> bool:
    """Restituisce True se l'email sembra automatica/sistema."""
    return bool(IGNORED_PATTERNS.search(email)) or domain in IGNORED_DOMAINS


def get_header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def extract_forwarded_sender(body: str) -> Optional[tuple[str, str, str]]:
    """
    Cerca il mittente originale in email forwarded.
    Formati comuni: "Da: Nome <email>" o "From: Nome <email>"
    """
    patterns = [
        r'(?:Da|From):\s*"?([^"<\n]+?)"?\s*<([^>\n]+)>',
        r'(?:Da|From):\s*([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})',
    ]
    for pat in patterns:
        m = re.search(pat, body, re.IGNORECASE | re.MULTILINE)
        if m:
            if len(m.groups()) == 2:
                name, email = m.group(1).strip(), m.group(2).strip().lower()
            else:
                name, email = "", m.group(1).strip().lower()
            domain = email.split("@")[-1] if "@" in email else ""
            if not is_automated(email, domain):
                return name, email, domain
    return None


def fetch_new_messages(service, last_history_id: Optional[str] = None,
                       max_results: int = 50) -> list[dict]:
    """
    Recupera i messaggi nuovi dall'inbox.
    Se disponibile usa history API per efficienza; altrimenti full scan.
    """
    messages = []
    try:
        if last_history_id:
            resp = service.users().history().list(
                userId="me",
                startHistoryId=last_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()
            history = resp.get("history", [])
            for item in history:
                for added in item.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []):
                        messages.append(msg)
        else:
            resp = service.users().messages().list(
                userId="me",
                labelIds=["INBOX"],
                maxResults=max_results,
            ).execute()
            messages = resp.get("messages", [])
    except Exception as e:
        log.error(f"Errore recupero messaggi Gmail: {e}")
    return messages


def parse_message(service, msg_id: str) -> Optional[ContactInfo]:
    """Scarica e analizza un singolo messaggio Gmail."""
    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
    except Exception as e:
        log.warning(f"Impossibile scaricare msg {msg_id}: {e}")
        return None

    headers   = msg.get("payload", {}).get("headers", [])
    from_hdr  = get_header(headers, "From")
    subject   = get_header(headers, "Subject")
    date_hdr  = get_header(headers, "Date")
    thread_id = msg.get("threadId", "")

    if not from_hdr:
        return None

    name, email, domain = extract_sender(from_hdr)

    # Se l'email diretta è automatica, cerca mittente originale nel body
    if is_automated(email, domain):
        # Estrai body testo
        body = _extract_body(msg.get("payload", {}))
        result = extract_forwarded_sender(body)
        if result:
            name, email, domain = result
        else:
            return None  # Nessun contatto reale trovato

    if is_automated(email, domain):
        return None

    first, last = parse_name(name)
    company     = company_from_domain(domain)

    return ContactInfo(
        email=email,
        first_name=first,
        last_name=last,
        company=company,
        domain=domain,
        subject=subject,
        thread_id=thread_id,
        received=date_hdr,
    )


def _extract_body(payload: dict) -> str:
    """Estrae il testo grezzo dal payload del messaggio."""
    import base64
    body = ""
    mime_type = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data", "")
    if data:
        try:
            body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
        except Exception:
            pass
    for part in payload.get("parts", []):
        body += _extract_body(part)
    return body


# ─────────────────────────────────────────────────────────────
# HubSpot
# ─────────────────────────────────────────────────────────────
def get_hubspot_client(api_key: str):
    """Restituisce il client HubSpot autenticato."""
    return hubspot.Client.create(access_token=api_key)


def find_contact(hs_client, email: str) -> Optional[dict]:
    """Cerca un contatto HubSpot per email. Restituisce None se non esiste."""
    try:
        resp = hs_client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "email",
                        "operator":     "EQ",
                        "value":        email,
                    }]
                }],
                "properties": [
                    "email", "firstname", "lastname",
                    "company", "hs_lead_source",
                ],
                "limit": 1,
            }
        )
        results = resp.results
        return results[0].to_dict() if results else None
    except HsApiException as e:
        log.error(f"Errore ricerca HubSpot ({email}): {e}")
        return None


def create_contact(hs_client, info: ContactInfo) -> Optional[str]:
    """Crea un nuovo contatto in HubSpot. Restituisce l'ID se ok."""
    props = {
        "email":           info.email,
        "hs_lead_source":  SOURCE_LABEL,
    }
    if info.first_name:
        props["firstname"] = info.first_name
    if info.last_name:
        props["lastname"]  = info.last_name
    if info.company:
        props["company"]   = info.company

    try:
        resp = hs_client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props, associations=[]
            )
        )
        return str(resp.id)
    except HsApiException as e:
        log.error(f"Errore creazione contatto {info.email}: {e}")
        return None


def update_contact(hs_client, contact_id: str, updates: dict) -> bool:
    """Aggiorna un contatto HubSpot con i campi mancanti."""
    if not updates:
        return True
    try:
        hs_client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(
                properties=updates
            ),
        )
        return True
    except HsApiException as e:
        log.error(f"Errore aggiornamento contatto {contact_id}: {e}")
        return False


def sync_contact(hs_client, info: ContactInfo) -> SyncResult:
    """
    Logica principale di sincronizzazione per un singolo contatto.
    Restituisce SyncResult con stato CREATO / AGGIORNATO / IGNORATO.
    """
    existing = find_contact(hs_client, info.email)

    if existing is None:
        # ── CREA NUOVO CONTATTO ─────────────────────────────
        new_id = create_contact(hs_client, info)
        if new_id:
            log.info(f"[CREATO]    {info.email} → HubSpot ID {new_id}")
            return SyncResult(email=info.email, status="CREATO", hubspot_id=new_id)
        else:
            return SyncResult(email=info.email, status="IGNORATO", reason="Errore creazione HubSpot")

    # ── AGGIORNA CONTATTO ESISTENTE ─────────────────────────
    contact_id = str(existing.get("id", ""))
    current    = existing.get("properties", {})
    updates    = {}

    # Aggiungi solo i campi mancanti/vuoti
    if not current.get("firstname") and info.first_name:
        updates["firstname"] = info.first_name
    if not current.get("lastname") and info.last_name:
        updates["lastname"] = info.last_name
    if not current.get("company") and info.company:
        updates["company"] = info.company
    if not current.get("hs_lead_source"):
        updates["hs_lead_source"] = SOURCE_LABEL

    if updates:
        ok = update_contact(hs_client, contact_id, updates)
        status = "AGGIORNATO" if ok else "IGNORATO"
        reason = f"Campi aggiornati: {list(updates.keys())}" if ok else "Errore aggiornamento"
        log.info(f"[{status}] {info.email} → HubSpot ID {contact_id} | {reason}")
        return SyncResult(email=info.email, status=status,
                          reason=reason, hubspot_id=contact_id)
    else:
        log.info(f"[IGNORATO]  {info.email} → già completo (ID {contact_id})")
        return SyncResult(email=info.email, status="IGNORATO",
                          reason="Nessun campo da aggiornare", hubspot_id=contact_id)


# ─────────────────────────────────────────────────────────────
# Loop principale
# ─────────────────────────────────────────────────────────────
def run_sync_loop(gmail_service, hs_client, poll_interval: int = POLL_INTERVAL):
    """
    Loop principale: controlla Gmail ogni `poll_interval` secondi,
    sincronizza i nuovi contatti in HubSpot e stampa il report.
    """
    log.info("▶  Gmail → HubSpot Sync avviato (Ctrl+C per fermare)")
    last_history_id: Optional[str] = None
    processed_ids: set[str] = set()

    while True:
        try:
            # ── Recupera messaggi ───────────────────────────
            messages = fetch_new_messages(gmail_service, last_history_id)

            results: list[SyncResult] = []

            for msg_stub in messages:
                msg_id = msg_stub.get("id", "")
                if msg_id in processed_ids:
                    continue
                processed_ids.add(msg_id)

                contact = parse_message(gmail_service, msg_id)
                if contact is None:
                    continue  # Automatico o non analizzabile

                result = sync_contact(hs_client, contact)
                results.append(result)

            # ── Report terminale ────────────────────────────
            if results:
                print("\n" + "─" * 60)
                print(f"  Sync completata — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print("─" * 60)
                print(f"  {'STATO':<12} {'EMAIL':<40} {'HubSpot ID'}")
                print("─" * 60)
                for r in results:
                    icon = {"CREATO": "🟢", "AGGIORNATO": "🔵", "IGNORATO": "⚪"}.get(r.status, "❓")
                    print(f"  {icon} {r.status:<10} {r.email:<40} {r.hubspot_id or '—'}")
                print("─" * 60 + "\n")

            # ── Aggiorna history_id per il prossimo ciclo ───
            if messages:
                try:
                    last_msg = gmail_service.users().messages().get(
                        userId="me",
                        id=messages[-1]["id"],
                        format="minimal",
                    ).execute()
                    last_history_id = last_msg.get("historyId")
                except Exception:
                    pass

            time.sleep(poll_interval)

        except KeyboardInterrupt:
            log.info("⏹  Sync interrotta dall'utente.")
            break
        except Exception as e:
            log.error(f"Errore nel loop principale: {e}")
            time.sleep(poll_interval)


# ─────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────
def main():
    import os, argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--hubspot-token", default=os.getenv("HUBSPOT_ACCESS_TOKEN"),
                        help="HubSpot Private App token (o var HUBSPOT_ACCESS_TOKEN)")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL,
                        help=f"Secondi tra un controllo e l'altro (default: {POLL_INTERVAL})")
    parser.add_argument("--once", action="store_true",
                        help="Esegui una sola volta e termina (utile per cron/debug)")
    args = parser.parse_args()

    if not args.hubspot_token:
        raise SystemExit("❌ Token HubSpot mancante. Usa --hubspot-token o la var HUBSPOT_ACCESS_TOKEN")

    gmail_service = get_gmail_service()
    hs_client     = get_hubspot_client(args.hubspot_token)

    if args.once:
        # Modalità singola esecuzione
        messages = fetch_new_messages(gmail_service, max_results=100)
        results  = []
        seen:set[str] = set()
        for msg_stub in messages:
            msg_id = msg_stub.get("id", "")
            contact = parse_message(gmail_service, msg_id)
            if contact and contact.email not in seen:
                seen.add(contact.email)
                results.append(sync_contact(hs_client, contact))
        # Stampa report JSON
        print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))
    else:
        run_sync_loop(gmail_service, hs_client, args.interval)


if __name__ == "__main__":
    main()
