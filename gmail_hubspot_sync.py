#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed estrae i contatti dai mittenti,
sincronizzandoli automaticamente in HubSpot.

Uso:
    python gmail_hubspot_sync.py              # esegui una volta
    python gmail_hubspot_sync.py --watch      # monitoraggio continuo
    python gmail_hubspot_sync.py --interval 5 # polling ogni 5 minuti
"""

import os
import re
import json
import time
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

from dotenv import load_dotenv
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import (
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)

# ──────────────────────────────────────────────
# Configurazione
# ──────────────────────────────────────────────

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("gmail_hubspot_sync.log"),
    ],
)
log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path("sync_state.json")
CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = Path("token.json")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Domini da ignorare (bot, notifiche, ecc.)
SKIP_DOMAINS = {
    "gmail.com",      # aggiungi eccezioni individuali in SKIP_EMAILS
    "noreply.com",
    "no-reply.com",
    "mailer-daemon",
    "accounts.google.com",
    "googlemail.com",
}

# Email specifiche da ignorare sempre
SKIP_EMAILS = {
    "noreply@accounts.google.com",
    "no-reply@accounts.google.com",
    "mailer-daemon@googlemail.com",
}


# ──────────────────────────────────────────────
# Dataclass contatto
# ──────────────────────────────────────────────

@dataclass
class SenderContact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""
    source_subject: str = ""

    def __post_init__(self):
        if not self.domain and "@" in self.email:
            self.domain = self.email.split("@", 1)[1]

    @property
    def is_personal_gmail(self) -> bool:
        return self.domain in ("gmail.com", "googlemail.com")

    def hubspot_props(self) -> dict:
        props = {
            "email": self.email,
            "lead_source": CONTACT_SOURCE,
        }
        if self.firstname:
            props["firstname"] = self.firstname.strip().title()
        if self.lastname:
            props["lastname"] = self.lastname.strip().title()
        if self.company:
            props["company"] = self.company
        return props


# ──────────────────────────────────────────────
# Stato / persistenza
# ──────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_sync_ts": None, "processed_thread_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ──────────────────────────────────────────────
# Gmail: autenticazione e fetch
# ──────────────────────────────────────────────

def gmail_auth() -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"File credenziali non trovato: {CREDENTIALS_FILE}\n"
                    "Scaricalo da Google Cloud Console → API & Services → Credentials"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def fetch_new_threads(service, since_ts: Optional[str] = None) -> list[dict]:
    """Restituisce i thread in arrivo più recenti di since_ts (Unix timestamp)."""
    query = "in:inbox -from:me"
    if since_ts:
        # Gmail accetta 'after:YYYY/MM/DD' oppure timestamp Unix interno
        # Usiamo la data formattata
        dt = datetime.fromtimestamp(int(since_ts), tz=timezone.utc)
        query += f" after:{dt.strftime('%Y/%m/%d')}"

    result = service.users().threads().list(
        userId="me", q=query, maxResults=50
    ).execute()
    return result.get("threads", [])


def get_thread_message(service, thread_id: str) -> Optional[dict]:
    """Restituisce il primo messaggio del thread con headers."""
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["From", "Subject", "Date"]
        ).execute()
        msgs = thread.get("messages", [])
        return msgs[0] if msgs else None
    except Exception as e:
        log.warning("Impossibile leggere thread %s: %s", thread_id, e)
        return None


# ──────────────────────────────────────────────
# Parsing mittente
# ──────────────────────────────────────────────

_FROM_RE = re.compile(r'"?([^"<]+)"?\s*<([^>]+)>', re.UNICODE)
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}')

def parse_sender(from_header: str, subject: str = "") -> Optional[SenderContact]:
    """
    Estrae email e nome dal campo From.
    Formato atteso: "Nome Cognome" <email@dominio.com>
                 oppure: email@dominio.com
    """
    from_header = from_header.strip()
    m = _FROM_RE.match(from_header)
    if m:
        raw_name = m.group(1).strip()
        email = m.group(2).strip().lower()
    else:
        em = _EMAIL_RE.search(from_header)
        if em:
            email = em.group(0).lower()
            raw_name = ""
        else:
            return None

    if email in SKIP_EMAILS:
        return None
    domain = email.split("@", 1)[1] if "@" in email else ""
    if domain in SKIP_DOMAINS and email not in SKIP_EMAILS:
        # Lascia passare gmail personali NON di sistema
        if domain in ("gmail.com", "googlemail.com") and "noreply" not in email:
            pass
        elif domain in SKIP_DOMAINS:
            return None

    # Normalizza nome
    parts = raw_name.split()
    firstname = parts[0] if parts else ""
    lastname = " ".join(parts[1:]) if len(parts) > 1 else ""

    # Azienda dal dominio (escludi gmail)
    company = ""
    if domain not in ("gmail.com", "googlemail.com"):
        company = domain_to_company(domain)

    return SenderContact(
        email=email,
        firstname=firstname,
        lastname=lastname,
        company=company,
        domain=domain,
        source_subject=subject,
    )


def domain_to_company(domain: str) -> str:
    """Converte un dominio email in un nome azienda leggibile."""
    # Rimuovi TLD e sottodomini noti
    parts = domain.replace(".it", "").replace(".com", "").replace(".org", "").replace(".net", "")
    # Rimuovi prefissi comuni
    for prefix in ("www.", "mail.", "info.", "press.", "ufficio.", "redazione."):
        parts = parts.removeprefix(prefix)
    # Capitalizza
    return " ".join(w.capitalize() for w in re.split(r"[.\-_]", parts) if w)


# ──────────────────────────────────────────────
# HubSpot: ricerca e upsert contatti
# ──────────────────────────────────────────────

def hs_client():
    if not HUBSPOT_API_KEY:
        raise ValueError("HUBSPOT_API_KEY non configurata nel file .env")
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


def find_contact_by_email(client, email: str) -> Optional[dict]:
    """Cerca un contatto per email in HubSpot. Restituisce None se non esiste."""
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "lead_source"],
    )
    try:
        res = client.crm.contacts.search_api.do_search(
            public_object_search_request=req
        )
        return res.results[0].to_dict() if res.results else None
    except ApiException as e:
        log.error("Errore ricerca contatto %s: %s", email, e)
        return None


def create_contact(client, contact: SenderContact) -> tuple[str, str]:
    """
    Crea un nuovo contatto in HubSpot.
    Restituisce (status, hubspot_id).
    """
    props = contact.hubspot_props()
    obj = SimplePublicObjectInputForCreate(properties=props)
    try:
        res = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        hs_id = res.id
        log.info("✅ CREATO   | %s | HubSpot ID: %s", contact.email, hs_id)
        return "Creato", hs_id
    except ApiException as e:
        body = json.loads(e.body) if e.body else {}
        if body.get("category") == "CONFLICT":
            # Il contatto esiste (race condition) → aggiorna
            existing = find_contact_by_email(client, contact.email)
            if existing:
                return update_contact(client, existing["id"], contact)
        log.error("Errore creazione %s: %s", contact.email, e)
        return "Errore", ""


def update_contact(client, hs_id: str, contact: SenderContact) -> tuple[str, str]:
    """
    Aggiorna un contatto esistente con i campi mancanti.
    Non sovrascrive dati già presenti (politica additive).
    """
    existing = client.crm.contacts.basic_api.get_by_id(
        contact_id=hs_id,
        properties=["email", "firstname", "lastname", "company", "lead_source"],
    ).to_dict()
    ex_props = existing.get("properties", {})

    updates = {}
    new_props = contact.hubspot_props()
    for key, val in new_props.items():
        if key == "email":
            continue
        if not ex_props.get(key) and val:
            updates[key] = val

    if not updates:
        log.info("⏭  IGNORATO | %s | HubSpot ID: %s (nessun aggiornamento)", contact.email, hs_id)
        return "Ignorato", hs_id

    try:
        from hubspot.crm.contacts import SimplePublicObjectInput
        obj = SimplePublicObjectInput(properties=updates)
        client.crm.contacts.basic_api.update(
            contact_id=hs_id, simple_public_object_input=obj
        )
        log.info("🔄 AGGIORNATO | %s | HubSpot ID: %s | campi: %s",
                 contact.email, hs_id, list(updates.keys()))
        return "Aggiornato", hs_id
    except ApiException as e:
        log.error("Errore aggiornamento %s: %s", contact.email, e)
        return "Errore", hs_id


def add_note(client, hs_id: str, subject: str, email: str):
    """Aggiunge una nota/attività email ricevuta al contatto."""
    try:
        note_body = (
            f"📧 Email ricevuta via Gmail\n"
            f"Da: {email}\n"
            f"Oggetto: {subject}\n"
            f"Data: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"Fonte: {CONTACT_SOURCE} | Tag: {CONTACT_TAG}"
        )
        from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput
        note = NoteInput(
            properties={
                "hs_note_body": note_body,
                "hs_timestamp": str(int(time.time() * 1000)),
            }
        )
        note_res = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note
        )
        # Associa al contatto
        client.crm.objects.notes.associations_api.create(
            note_id=note_res.id,
            to_object_type="contacts",
            to_object_id=hs_id,
            association_type="note_to_contact",
        )
    except Exception as e:
        log.debug("Nota non aggiunta per %s: %s", email, e)


# ──────────────────────────────────────────────
# Core sync loop
# ──────────────────────────────────────────────

def sync_once(gmail_service, hs, state: dict, dry_run: bool = False) -> list[dict]:
    """
    Esegue una passata di sincronizzazione.
    Restituisce lista dei risultati per ogni email processata.
    """
    results = []
    processed_ids: set = set(state.get("processed_thread_ids", []))
    since_ts = state.get("last_sync_ts")

    threads = fetch_new_threads(gmail_service, since_ts)
    log.info("Trovati %d thread da analizzare", len(threads))

    newest_ts = int(since_ts) if since_ts else 0

    for t in threads:
        thread_id = t["id"]
        if thread_id in processed_ids:
            continue

        msg = get_thread_message(gmail_service, thread_id)
        if not msg:
            processed_ids.add(thread_id)
            continue

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "")
        date_header = headers.get("Date", "")

        # Timestamp del messaggio
        internal_date = int(msg.get("internalDate", 0)) // 1000
        if internal_date > newest_ts:
            newest_ts = internal_date

        contact = parse_sender(from_header, subject)
        if not contact:
            log.debug("Saltato (mittente sistema): %s", from_header)
            processed_ids.add(thread_id)
            continue

        log.info("📨 Elaboro: %s (%s)", contact.email, contact.firstname or "senza nome")

        if dry_run:
            results.append({
                "status": "DryRun",
                "email": contact.email,
                "nome": f"{contact.firstname} {contact.lastname}".strip(),
                "azienda": contact.company,
                "hubspot_id": "-",
            })
            processed_ids.add(thread_id)
            continue

        # Cerca contatto in HubSpot
        existing = find_contact_by_email(hs, contact.email)
        if existing:
            status, hs_id = update_contact(hs, existing["id"], contact)
        else:
            status, hs_id = create_contact(hs, contact)

        # Aggiungi nota attività
        if hs_id and status != "Ignorato":
            add_note(hs, hs_id, subject, contact.email)

        results.append({
            "status": status,
            "email": contact.email,
            "nome": f"{contact.firstname} {contact.lastname}".strip(),
            "azienda": contact.company,
            "hubspot_id": hs_id,
        })
        processed_ids.add(thread_id)

    # Aggiorna stato
    state["processed_thread_ids"] = list(processed_ids)[-500:]  # tieni ultimi 500
    if newest_ts > 0:
        state["last_sync_ts"] = str(newest_ts)

    return results


def print_results(results: list[dict]):
    if not results:
        print("\nNessuna nuova email da processare.")
        return
    print("\n" + "─" * 80)
    print(f"{'STATO':<12} {'EMAIL':<40} {'NOME':<25} {'HUBSPOT ID'}")
    print("─" * 80)
    for r in results:
        icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭ ", "Errore": "❌", "DryRun": "🔍"}.get(r["status"], "?")
        print(f"{icon} {r['status']:<10} {r['email']:<40} {r['nome']:<25} {r['hubspot_id']}")
    print("─" * 80)
    total = len(results)
    creati = sum(1 for r in results if r["status"] == "Creato")
    aggiornati = sum(1 for r in results if r["status"] == "Aggiornato")
    ignorati = sum(1 for r in results if r["status"] == "Ignorato")
    print(f"Totale: {total} | ✅ Creati: {creati} | 🔄 Aggiornati: {aggiornati} | ⏭  Ignorati: {ignorati}\n")


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--watch", action="store_true",
                        help="Modalità monitoraggio continuo (polling)")
    parser.add_argument("--interval", type=int, default=10,
                        help="Intervallo di polling in minuti (default: 10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simula senza scrivere su HubSpot")
    parser.add_argument("--reset", action="store_true",
                        help="Azzera lo stato e riprocessa tutte le email")
    args = parser.parse_args()

    # Autenticazione Gmail
    log.info("Autenticazione Gmail...")
    creds = gmail_auth()
    gmail_service = build("gmail", "v1", credentials=creds)

    # Client HubSpot
    if not args.dry_run:
        hs = hs_client()
    else:
        hs = None
        log.info("Modalità DRY RUN attiva — nessuna scrittura su HubSpot")

    # Stato
    state = {} if args.reset else load_state()

    if args.watch:
        log.info("▶ Monitoraggio continuo ogni %d minuti. Ctrl+C per fermare.", args.interval)
        while True:
            try:
                results = sync_once(gmail_service, hs, state, dry_run=args.dry_run)
                print_results(results)
                save_state(state)
                log.info("Prossima esecuzione tra %d minuti...", args.interval)
                time.sleep(args.interval * 60)
            except KeyboardInterrupt:
                log.info("Monitoraggio interrotto dall'utente.")
                break
            except Exception as e:
                log.error("Errore durante la sincronizzazione: %s", e, exc_info=True)
                time.sleep(60)  # aspetta 1 minuto prima di riprovare
    else:
        results = sync_once(gmail_service, hs, state, dry_run=args.dry_run)
        print_results(results)
        save_state(state)


if __name__ == "__main__":
    main()
