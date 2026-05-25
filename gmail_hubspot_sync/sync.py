"""
Gmail → HubSpot Contact Sync
==============================
Monitora le email in arrivo su Gmail, estrae i mittenti unici
e li sincronizza automaticamente come contatti in HubSpot.

Funzionalità:
- Estrazione mittente reale da email inoltrate (forward)
- Verifica duplicati via email come chiave unica
- Crea o aggiorna contatti HubSpot
- Aggiunge fonte "Gmail" e tag "Inbound Gmail"
- Report per ogni email processata: Creato / Aggiornato / Ignorato

Dipendenze:
    pip install google-auth google-auth-oauthlib google-auth-httplib2
                google-api-python-client hubspot-api-client python-dotenv

Configurazione:
    Copia .env.example → .env e compila le credenziali.
"""

from __future__ import annotations

import os
import re
import json
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Optional

from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# Config & logging
# ──────────────────────────────────────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hubspot_sync")

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE       = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_API_KEY        = os.getenv("HUBSPOT_API_KEY", "")
GMAIL_SCOPES           = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL_SECONDS  = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
GMAIL_QUERY            = os.getenv("GMAIL_QUERY", "in:inbox -from:me newer_than:1d")
INBOUND_TAG            = "Inbound Gmail"
CONTACT_SOURCE         = "OFFLINE_SOURCES"   # HubSpot lead_source enum value

# Indirizzi da ignorare (relay interni, sistemi automatici, ecc.)
IGNORE_EMAILS = {
    "no-reply@accounts.google.com",
    "noreply@accounts.google.com",
    "mailer-daemon@googlemail.com",
}

# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Contact:
    email: str
    firstname: str = ""
    lastname: str  = ""
    company: str   = ""
    source: str    = "Gmail"
    raw_name: str  = ""

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""

    @property
    def company_from_domain(self) -> str:
        """Ricava nome azienda dal dominio (rimuove TLD e www)."""
        domain = self.domain
        parts = domain.split(".")
        # Rimuove TLD comuni e sottodomini
        skip = {"it", "com", "org", "net", "eu", "info", "biz", "ch", "gov"}
        name_parts = [p for p in parts if p.lower() not in skip and p != "www"]
        return name_parts[0].replace("-", " ").title() if name_parts else domain


@dataclass
class SyncResult:
    email: str
    status: str          # "CREATO" | "AGGIORNATO" | "IGNORATO"
    contact_id: str = ""
    reason: str     = ""


# ──────────────────────────────────────────────────────────────────────────────
# Gmail client
# ──────────────────────────────────────────────────────────────────────────────

class GmailClient:
    """Wrapper attorno alla Gmail API v1."""

    def __init__(self):
        self.service = self._build_service()

    def _build_service(self):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Librerie Google mancanti. Installa: pip install google-auth "
                "google-auth-oauthlib google-api-python-client"
            ) from exc

        creds = None
        if os.path.exists(GMAIL_TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(GMAIL_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def list_threads(self, query: str, max_results: int = 50) -> list[dict]:
        """Ritorna thread Gmail filtrati dalla query."""
        response = (
            self.service.users()
            .threads()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return response.get("threads", [])

    def get_thread(self, thread_id: str) -> dict:
        return (
            self.service.users()
            .threads()
            .get(userId="me", id=thread_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )


# ──────────────────────────────────────────────────────────────────────────────
# Parsing mittenti
# ──────────────────────────────────────────────────────────────────────────────

# Pattern per estrarre mittente reale da corpo di email inoltrata italiana
# Es: Da "Helel Fiori" helelfiori@hotmail.it
_FWD_IT = re.compile(
    r'Da\s+"?([^"<\n]+?)"?\s+<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
    re.IGNORECASE,
)
# Fallback inglese: From: "Name" <email>
_FWD_EN = re.compile(
    r'From:\s+"?([^"<\n]+?)"?\s+<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
    re.IGNORECASE,
)


def parse_name(raw: str) -> tuple[str, str]:
    """Divide raw_name in (firstname, lastname) in modo euristico."""
    raw = raw.strip().strip('"').strip()
    parts = raw.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    # Mantiene il cognome come ultimo token
    return " ".join(parts[:-1]), parts[-1]


def extract_contact_from_snippet(snippet: str, sender_header: str) -> Optional[Contact]:
    """
    Prova prima ad estrarre il mittente originale da email inoltrata,
    poi ricade sull'header From standard.
    """
    # Prova pattern forward italiano
    m = _FWD_IT.search(snippet) or _FWD_EN.search(snippet)
    if m:
        raw_name, email = m.group(1).strip(), m.group(2).strip().lower()
    else:
        # Usa l'header From diretto
        raw_name, email = parseaddr(sender_header)
        email = email.lower()

    if not email or email in IGNORE_EMAILS:
        return None

    firstname, lastname = parse_name(raw_name)
    c = Contact(
        email=email,
        firstname=firstname,
        lastname=lastname,
        raw_name=raw_name,
    )
    c.company = c.company_from_domain
    return c


# ──────────────────────────────────────────────────────────────────────────────
# HubSpot client
# ──────────────────────────────────────────────────────────────────────────────

class HubSpotClient:
    """Wrapper attorno alle API HubSpot v3 Contacts."""

    BASE = "https://api.hubapi.com"

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("HUBSPOT_API_KEY non configurata.")
        try:
            import hubspot
            from hubspot import HubSpot
            from hubspot.crm.contacts import ApiException
        except ImportError as exc:
            raise RuntimeError(
                "hubspot-api-client mancante. Installa: pip install hubspot-api-client"
            ) from exc

        from hubspot import HubSpot
        self._client = HubSpot(access_token=api_key)

    def find_by_email(self, email: str) -> Optional[dict]:
        """Cerca contatto per email. Ritorna il record o None."""
        from hubspot.crm.contacts import ApiException
        try:
            resp = self._client.crm.contacts.basic_api.get_by_id(
                email,
                id_property="email",
                properties=["email", "firstname", "lastname", "company",
                            "hs_lead_status", "leadsource"],
                archived=False,
            )
            return {"id": resp.id, "properties": resp.properties}
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def create_contact(self, contact: Contact) -> str:
        """Crea un nuovo contatto. Ritorna l'ID."""
        from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
        props = self._build_props(contact)
        obj = SimplePublicObjectInputForCreate(properties=props)
        result = self._client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        log.info("Contatto CREATO → %s (ID: %s)", contact.email, result.id)
        return result.id

    def update_contact(self, contact_id: str, contact: Contact,
                       existing_props: dict) -> None:
        """Aggiorna i campi mancanti su un contatto esistente."""
        from hubspot.crm.contacts import SimplePublicObjectInput
        updates = {}
        new_props = self._build_props(contact)
        for key, val in new_props.items():
            if val and not existing_props.get(key):
                updates[key] = val
        if not updates:
            log.info("Contatto già aggiornato, nessuna modifica → %s", contact.email)
            return
        obj = SimplePublicObjectInput(properties=updates)
        self._client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=obj
        )
        log.info("Contatto AGGIORNATO → %s (ID: %s) campi: %s",
                 contact.email, contact_id, list(updates.keys()))

    def _build_props(self, contact: Contact) -> dict:
        props: dict[str, str] = {
            "email":       contact.email,
            "leadsource":  "OFFLINE_SOURCES",  # HubSpot enum "Offline sources"
        }
        if contact.firstname:
            props["firstname"] = contact.firstname
        if contact.lastname:
            props["lastname"] = contact.lastname
        if contact.company:
            props["company"] = contact.company
        return props


# ──────────────────────────────────────────────────────────────────────────────
# Sync engine
# ──────────────────────────────────────────────────────────────────────────────

class GmailHubSpotSync:
    def __init__(self):
        self.gmail   = GmailClient()
        self.hubspot = HubSpotClient(HUBSPOT_API_KEY)
        self._seen_emails: set[str] = set()   # deduplicazione in-session

    def process_threads(self, query: str = GMAIL_QUERY) -> list[SyncResult]:
        """Processa tutti i thread corrispondenti alla query e ritorna i risultati."""
        threads = self.gmail.list_threads(query)
        log.info("Thread trovati: %d", len(threads))

        results: list[SyncResult] = []
        contacts_seen: dict[str, Contact] = {}

        # ── 1. Estrai contatti unici dai thread ─────────────────────────────
        for thread in threads:
            try:
                detail = self.gmail.get_thread(thread["id"])
                for msg in detail.get("messages", []):
                    headers = {h["name"]: h["value"]
                               for h in msg.get("payload", {}).get("headers", [])}
                    snippet = detail.get("snippet", "")
                    sender_header = headers.get("From", "")

                    contact = extract_contact_from_snippet(snippet, sender_header)
                    if contact and contact.email not in contacts_seen:
                        contacts_seen[contact.email] = contact
            except Exception as exc:
                log.warning("Errore thread %s: %s", thread["id"], exc)

        log.info("Contatti unici estratti: %d", len(contacts_seen))

        # ── 2. Sync HubSpot ──────────────────────────────────────────────────
        for email, contact in contacts_seen.items():
            if email in IGNORE_EMAILS:
                results.append(SyncResult(email=email, status="IGNORATO",
                                          reason="Email di sistema"))
                continue

            try:
                existing = self.hubspot.find_by_email(email)
                if existing:
                    contact_id = existing["id"]
                    self.hubspot.update_contact(contact_id, contact,
                                                existing["properties"])
                    results.append(SyncResult(email=email, status="AGGIORNATO",
                                              contact_id=contact_id))
                else:
                    contact_id = self.hubspot.create_contact(contact)
                    results.append(SyncResult(email=email, status="CREATO",
                                              contact_id=contact_id))
                time.sleep(0.1)  # rate limiting gentile
            except Exception as exc:
                log.error("Errore sync %s: %s", email, exc)
                results.append(SyncResult(email=email, status="IGNORATO",
                                          reason=str(exc)))

        return results

    def run_forever(self):
        """Loop continuo: processa nuove email ogni POLL_INTERVAL_SECONDS."""
        log.info("▶ Gmail → HubSpot Sync avviato (intervallo: %ds)", POLL_INTERVAL_SECONDS)
        while True:
            try:
                results = self.process_threads()
                self._print_report(results)
            except Exception as exc:
                log.error("Errore ciclo principale: %s", exc)
            log.info("⏱  Prossima esecuzione tra %ds...", POLL_INTERVAL_SECONDS)
            time.sleep(POLL_INTERVAL_SECONDS)

    @staticmethod
    def _print_report(results: list[SyncResult]) -> None:
        print("\n" + "═" * 65)
        print(f"{'STATO':<12} {'EMAIL CONTATTO':<38} {'ID HUBSPOT'}")
        print("─" * 65)
        counts = {"CREATO": 0, "AGGIORNATO": 0, "IGNORATO": 0}
        for r in sorted(results, key=lambda x: x.status):
            icon = {"CREATO": "✅", "AGGIORNATO": "🔄", "IGNORATO": "⏭ "}.get(r.status, "?")
            print(f"{icon} {r.status:<10} {r.email:<38} {r.contact_id or r.reason}")
            counts[r.status] = counts.get(r.status, 0) + 1
        print("─" * 65)
        print(f"Totale: {len(results)} | "
              f"✅ Creati: {counts['CREATO']} | "
              f"🔄 Aggiornati: {counts['AGGIORNATO']} | "
              f"⏭  Ignorati: {counts['IGNORATO']}")
        print("═" * 65 + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sync = GmailHubSpotSync()
    sync.run_forever()
