#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitora le email in arrivo su Gmail, estrae i mittenti
e li sincronizza automaticamente in HubSpot come contatti.

Autore: Cristian Mameli
Data:   2026-05-26
"""

import re
import time
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

# ── dipendenze ──────────────────────────────────────────────────────────────
# pip install google-auth google-auth-oauthlib google-auth-httplib2
#             google-api-python-client hubspot-api-client
import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    ApiException,
)

# ── configurazione ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("gmail_hubspot_sync")

POLL_INTERVAL_SECONDS = 60          # controlla ogni minuto
GMAIL_QUERY = "in:inbox -from:me -in:draft"  # filtra email in arrivo
CONTACT_SOURCE = "Gmail"            # valore per hs_lead_source / leadsource
GMAIL_TAG = "Inbound Gmail"         # tag da aggiungere

# domini da ignorare (interni / no-reply)
IGNORED_DOMAINS = {
    "gmail.com",   # escludi per default i mittenti @gmail.com generici
    "noreply.com", "no-reply.com", "mailer-daemon.org",
}
# email specifiche da ignorare
IGNORED_EMAILS = set()              # es. {"noreply@example.com"}


# ── strutture dati ────────────────────────────────────────────────────────────
class SyncStatus(Enum):
    CREATED = "✅ Creato"
    UPDATED = "🔄 Aggiornato"
    SKIPPED = "⏭  Ignorato"
    ERROR   = "❌ Errore"


@dataclass
class ContactInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = field(init=False)

    def __post_init__(self):
        self.domain = self.email.split("@")[-1] if "@" in self.email else ""

    def company_from_domain(self) -> str:
        """Ricava il nome azienda dal dominio (euristica semplice)."""
        if self.company:
            return self.company
        parts = self.domain.split(".")
        # rimuovi TLD comuni e "www"
        stop = {"com", "it", "org", "net", "gov", "eu", "ch",
                "co", "io", "ai", "info", "biz", "edu", "www"}
        names = [p for p in parts if p.lower() not in stop and len(p) > 1]
        return names[0].title() if names else ""


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str] = None
    error: Optional[str] = None

    def __str__(self):
        base = f"{self.status.value} | {self.email} | ID: {self.hubspot_id or '—'}"
        return base + (f" | ⚠ {self.error}" if self.error else "")


# ── parsing mittenti ──────────────────────────────────────────────────────────
# pattern: Da "Nome Cognome" email@domain.tld
_FW_PATTERN = re.compile(
    r'Da\s+"?([^"<\n]+?)"?\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)
_EMAIL_PATTERN = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
)


def parse_name_from_display(display: str) -> tuple[str, str]:
    """Separa firstname / lastname da una stringa display name."""
    parts = display.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def extract_contacts_from_message(msg: dict) -> list[ContactInfo]:
    """
    Estrae i contatti da un messaggio Gmail.
    Gestisce sia mittenti diretti che email inoltrate (Fw:).
    """
    contacts: list[ContactInfo] = []

    # 1) mittente diretto
    sender_raw = msg.get("sender", "") or ""
    direct_emails = _EMAIL_PATTERN.findall(sender_raw)
    for em in direct_emails:
        if em.lower() in IGNORED_EMAILS:
            continue
        # prova a estrarre il nome dalla stringa "Nome <email>"
        display = re.sub(r'<[^>]+>', '', sender_raw).strip().strip('"')
        fn, ln = parse_name_from_display(display) if display else ("", "")
        contacts.append(ContactInfo(email=em.lower(), firstname=fn, lastname=ln))

    # 2) mittente originale nelle email inoltrate (pattern italiano "Da ...")
    snippet = msg.get("snippet", "") or ""
    for m in _FW_PATTERN.finditer(snippet):
        display_name, em = m.group(1).strip(), m.group(2).strip().lower()
        if em in IGNORED_EMAILS:
            continue
        fn, ln = parse_name_from_display(display_name)
        # cerca di ricavare azienda dal display name se contiene parole chiave
        company = ""
        if any(kw in display_name.lower()
               for kw in ("ufficio", "studio", "agenzia", "media",
                           "comunicazione", "stampa", "redazione")):
            company = display_name
        contacts.append(ContactInfo(
            email=em, firstname=fn, lastname=ln, company=company
        ))

    # 3) deduplicazione per email
    seen: dict[str, ContactInfo] = {}
    for c in contacts:
        if c.email not in seen:
            seen[c.email] = c
    return list(seen.values())


def should_skip(contact: ContactInfo) -> bool:
    """Ritorna True se il contatto va ignorato."""
    return (
        not contact.email
        or contact.domain in IGNORED_DOMAINS
        or contact.email in IGNORED_EMAILS
    )


# ── HubSpot helpers ───────────────────────────────────────────────────────────
def search_contact(hs_client, email: str) -> Optional[dict]:
    """Cerca un contatto per email. Ritorna il record o None."""
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company",
                    "leadsource", "hs_tag_ids"],
    )
    try:
        resp = hs_client.crm.contacts.search_api.do_search(req)
        return resp.results[0].to_dict() if resp.results else None
    except ApiException as e:
        log.error("Errore ricerca HubSpot per %s: %s", email, e)
        return None


def build_properties(contact: ContactInfo, existing: Optional[dict] = None) -> dict:
    """Costruisce il dizionario properties da inviare a HubSpot."""
    props: dict[str, str] = {}
    ex = (existing or {}).get("properties", {})

    def set_if_missing(key: str, value: str):
        if value and not ex.get(key):
            props[key] = value

    set_if_missing("firstname",  contact.firstname)
    set_if_missing("lastname",   contact.lastname)
    set_if_missing("company",    contact.company or contact.company_from_domain())
    set_if_missing("leadsource", CONTACT_SOURCE)
    # tag come nota aggiuntiva (HubSpot standard)
    set_if_missing("hs_lead_status", GMAIL_TAG)
    return props


def create_contact(hs_client, contact: ContactInfo) -> SyncResult:
    """Crea un nuovo contatto in HubSpot."""
    props = build_properties(contact)
    props["email"] = contact.email
    try:
        obj = SimplePublicObjectInputForCreate(properties=props)
        resp = hs_client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        log.info("Creato contatto: %s (ID %s)", contact.email, resp.id)
        return SyncResult(SyncStatus.CREATED, contact.email, resp.id)
    except ApiException as e:
        body = json.loads(e.body) if e.body else {}
        # conflitto: il contatto esiste già → aggiorna
        if body.get("category") == "CONFLICT":
            vid = body.get("message", "").split("existing ID: ")[-1]
            return update_contact(hs_client, vid, contact, existing=None)
        return SyncResult(SyncStatus.ERROR, contact.email, error=str(e))


def update_contact(
    hs_client, contact_id: str, contact: ContactInfo, existing: Optional[dict]
) -> SyncResult:
    """Aggiorna un contatto esistente con i campi mancanti."""
    props = build_properties(contact, existing)
    if not props:
        return SyncResult(SyncStatus.SKIPPED, contact.email, contact_id)
    try:
        obj = SimplePublicObjectInput(properties=props)
        hs_client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=obj,
        )
        log.info("Aggiornato contatto: %s (ID %s), campi: %s",
                 contact.email, contact_id, list(props.keys()))
        return SyncResult(SyncStatus.UPDATED, contact.email, contact_id)
    except ApiException as e:
        return SyncResult(SyncStatus.ERROR, contact.email, contact_id, str(e))


def sync_contact(hs_client, contact: ContactInfo) -> SyncResult:
    """Pipeline principale: cerca → crea o aggiorna."""
    if should_skip(contact):
        return SyncResult(SyncStatus.SKIPPED, contact.email,
                          error="dominio/email ignorato")

    existing = search_contact(hs_client, contact.email)
    if existing:
        return update_contact(hs_client, existing["id"], contact, existing)
    return create_contact(hs_client, contact)


# ── Gmail polling ─────────────────────────────────────────────────────────────
def build_gmail_service():
    """
    Costruisce il client Gmail usando Application Default Credentials.
    Per eseguire localmente, autentica con:
        gcloud auth application-default login
    oppure usa un service account con dominio delegato.
    """
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/gmail.readonly"]
    )
    return build("gmail", "v1", credentials=creds)


def fetch_new_messages(gmail_svc, last_history_id: Optional[str],
                       query: str = GMAIL_QUERY) -> tuple[list[dict], str]:
    """
    Recupera i nuovi messaggi dall'ultima sincronizzazione.
    Usa la Gmail History API quando disponibile, altrimenti caduta su search.
    Ritorna (messages, new_history_id).
    """
    messages: list[dict] = []
    new_history_id = last_history_id

    if last_history_id:
        try:
            resp = gmail_svc.users().history().list(
                userId="me",
                startHistoryId=last_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()
            records = resp.get("history", [])
            new_history_id = resp.get("historyId", last_history_id)
            for record in records:
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []):
                        messages.append(msg)
            return messages, new_history_id
        except HttpError as e:
            log.warning("History API non disponibile (%s), uso search.", e)

    # fallback: cerca le ultime email non lette
    try:
        resp = gmail_svc.users().messages().list(
            userId="me", q=query, maxResults=50
        ).execute()
        raw_msgs = resp.get("messages", [])
        # recupera i dettagli (sender, snippet)
        for m in raw_msgs:
            detail = gmail_svc.users().messages().get(
                userId="me", id=m["id"],
                format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"]: h["value"]
                       for h in detail.get("payload", {}).get("headers", [])}
            messages.append({
                "id": m["id"],
                "sender": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "snippet": detail.get("snippet", ""),
                "labelIds": detail.get("labelIds", []),
            })
        # prendi lo historyId dall'ultimo messaggio per le chiamate successive
        if raw_msgs:
            last = gmail_svc.users().messages().get(
                userId="me", id=raw_msgs[0]["id"], format="minimal"
            ).execute()
            new_history_id = last.get("historyId", last_history_id)
    except HttpError as e:
        log.error("Errore fetch Gmail: %s", e)

    return messages, new_history_id


# ── loop principale ───────────────────────────────────────────────────────────
def run_sync_loop(
    gmail_token: str,          # HubSpot Private App Token
    poll_interval: int = POLL_INTERVAL_SECONDS,
    once: bool = False,        # True = esegui una sola volta (utile per test)
):
    """
    Loop principale di sincronizzazione.

    Args:
        gmail_token:   Token HubSpot Private App.
        poll_interval: Secondi tra una verifica e l'altra.
        once:          Se True, esegue una sola iterazione e ritorna.
    """
    hs_client = hubspot.Client.create(access_token=gmail_token)
    gmail_svc = build_gmail_service()

    last_history_id: Optional[str] = None
    processed_ids: set[str] = set()   # evita di processare lo stesso msg due volte

    log.info("▶  Gmail → HubSpot Sync avviato. Intervallo: %ds", poll_interval)

    while True:
        log.info("🔍 Controllo nuovi messaggi Gmail…")
        messages, last_history_id = fetch_new_messages(
            gmail_svc, last_history_id
        )
        new_msgs = [m for m in messages if m.get("id") not in processed_ids]
        log.info("   %d messaggi nuovi trovati.", len(new_msgs))

        results: list[SyncResult] = []
        seen_emails_this_run: set[str] = set()

        for msg in new_msgs:
            processed_ids.add(msg.get("id", ""))
            contacts = extract_contacts_from_message(msg)
            for contact in contacts:
                if contact.email in seen_emails_this_run:
                    continue
                seen_emails_this_run.add(contact.email)
                result = sync_contact(hs_client, contact)
                results.append(result)

        # ── report ────────────────────────────────────────────────────────
        if results:
            print("\n" + "═" * 70)
            print(f"  REPORT SINCRONIZZAZIONE — {datetime.now():%Y-%m-%d %H:%M:%S}")
            print("═" * 70)
            print(f"  {'STATO':<20} {'EMAIL':<40} {'ID HUBSPOT'}")
            print("─" * 70)
            for r in results:
                hs_id = r.hubspot_id or "—"
                print(f"  {r.status.value:<20} {r.email:<40} {hs_id}")
            print("═" * 70 + "\n")

        if once:
            return results

        log.info("⏱  Prossima verifica tra %ds…", poll_interval)
        time.sleep(poll_interval)


# ── entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--token", default=os.environ.get("HUBSPOT_TOKEN", ""),
        help="HubSpot Private App Token (o env HUBSPOT_TOKEN)"
    )
    parser.add_argument(
        "--interval", type=int, default=POLL_INTERVAL_SECONDS,
        help=f"Secondi tra i controlli (default: {POLL_INTERVAL_SECONDS})"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Esegui una sola volta e termina"
    )
    args = parser.parse_args()

    if not args.token:
        parser.error("Specifica --token o esporta HUBSPOT_TOKEN=<token>")

    run_sync_loop(
        gmail_token=args.token,
        poll_interval=args.interval,
        once=args.once,
    )
