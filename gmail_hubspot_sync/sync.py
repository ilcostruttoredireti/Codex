#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed estrae i mittenti per sincronizzarli in HubSpot.
Evita duplicati usando l'email come chiave unica.
"""

import os
import re
import time
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from email.utils import parseaddr

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sync.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GMAIL_SCOPES        = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE    = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_CREDS_FILE    = os.getenv("GMAIL_CREDS_FILE", "credentials.json")
HUBSPOT_API_KEY     = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL    = "https://api.hubapi.com"
POLL_INTERVAL_SEC   = int(os.getenv("POLL_INTERVAL_SEC", "300"))   # default 5 min
STATE_FILE          = os.getenv("STATE_FILE", "sync_state.json")   # persiste l'ultimo historyId Gmail

# Email da ignorare (noreply, mailer-daemon, propri indirizzi, ecc.)
SKIP_PATTERNS = [
    r"no.?reply@",
    r"noreply@",
    r"mailer-daemon@",
    r"@googlemail\.com",
    r"analytics-noreply@",
    r"postmaster@",
    r"bounce[s]?@",
    r"notifications?@",
]

SKIP_DOMAINS = {
    "googlemail.com",
    "bounce.email",
}


# ── Dataclass ─────────────────────────────────────────────────────────────────
@dataclass
class ContactInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""
    source_subject: str = ""

    def __post_init__(self):
        if "@" in self.email and not self.domain:
            self.domain = self.email.split("@", 1)[1].lower()
        if not self.company and self.domain:
            self.company = self._domain_to_company(self.domain)

    @staticmethod
    def _domain_to_company(domain: str) -> str:
        """Deriva un nome azienda approssimativo dal dominio."""
        # Rimuovi TLD comuni e www
        parts = domain.replace("www.", "").split(".")
        if len(parts) >= 2:
            name = parts[-2]              # es. "gliamicidellebici" da gliamicidellebici.it
        else:
            name = parts[0]
        return name.replace("-", " ").replace("_", " ").title()


@dataclass
class SyncResult:
    email: str
    status: str          # "CREATO" | "AGGIORNATO" | "IGNORATO"
    hubspot_id: Optional[str] = None
    reason: str = ""


# ── Gmail client ─────────────────────────────────────────────────────────────
class GmailClient:
    def __init__(self):
        self.service = self._build_service()

    def _build_service(self):
        creds = None
        if os.path.exists(GMAIL_TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDS_FILE, GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
            with open(GMAIL_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def get_history_id(self) -> str:
        """Restituisce l'historyId corrente della mailbox."""
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def list_new_messages(self, start_history_id: str) -> list[dict]:
        """
        Usa l'History API per recuperare solo i messaggi aggiunti alla INBOX
        dall'ultimo controllo.
        """
        messages = []
        try:
            result = self.service.users().history().list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()
            for record in result.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    # Filtra solo INBOX (esclude SENT, DRAFT, ecc.)
                    if "INBOX" in msg.get("labelIds", []):
                        messages.append(msg)
        except Exception as e:
            log.warning("History API error: %s. Fallback su query recente.", e)
            messages = self._fallback_recent(minutes=int(POLL_INTERVAL_SEC / 60) + 2)
        return messages

    def _fallback_recent(self, minutes: int = 10) -> list[dict]:
        """Fallback: cerca messaggi in INBOX più recenti di N minuti."""
        results = self.service.users().messages().list(
            userId="me",
            q=f"in:inbox newer_than:{max(1, minutes // 1440)}d",
            maxResults=50,
        ).execute()
        return results.get("messages", [])

    def get_sender(self, message_id: str) -> Optional[ContactInfo]:
        """Recupera i metadati From/Subject di un messaggio."""
        try:
            msg = self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
            raw_from = headers.get("From", "")
            subject  = headers.get("Subject", "")
            if not raw_from:
                return None
            display_name, email_addr = parseaddr(raw_from)
            email_addr = email_addr.strip().lower()
            if not email_addr or "@" not in email_addr:
                return None
            if _should_skip(email_addr):
                return None
            first, last = _parse_display_name(display_name)
            return ContactInfo(
                email=email_addr,
                first_name=first,
                last_name=last,
                source_subject=subject[:200],
            )
        except Exception as e:
            log.warning("Impossibile recuperare messaggio %s: %s", message_id, e)
            return None


# ── HubSpot client ───────────────────────────────────────────────────────────
class HubSpotClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Cerca un contatto per email; restituisce il record o None."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "email",
                    "operator": "EQ",
                    "value": email,
                }]
            }],
            "properties": ["email", "firstname", "lastname", "company",
                           "hs_analytics_source", "createdate"],
            "limit": 1,
        }
        resp = self.session.post(url, json=payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, contact: ContactInfo) -> str:
        """Crea un nuovo contatto; restituisce l'ID HubSpot."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts"
        payload = {"properties": _build_properties(contact)}
        resp = self.session.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()["id"]

    def update_contact(self, contact_id: str, props: dict) -> None:
        """Aggiorna i campi mancanti di un contatto esistente."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}"
        resp = self.session.patch(url, json={"properties": props})
        resp.raise_for_status()

    def create_note(self, contact_id: str, subject: str, body: str) -> None:
        """Associa una nota/attività al contatto."""
        note_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/notes"
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        note_payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(now_ms),
            },
            "associations": [{
                "to":   {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                           "associationTypeId": 202}],   # Note → Contact
            }],
        }
        resp = self.session.post(note_url, json=note_payload)
        if not resp.ok:
            log.warning("Nota non creata per %s: %s", contact_id, resp.text)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _should_skip(email: str) -> bool:
    domain = email.split("@", 1)[1] if "@" in email else ""
    if domain in SKIP_DOMAINS:
        return True
    for pat in SKIP_PATTERNS:
        if re.search(pat, email, re.IGNORECASE):
            return True
    return False


def _parse_display_name(name: str) -> tuple[str, str]:
    """Tenta di dividere 'Nome Cognome' in (first, last)."""
    name = name.strip().strip('"')
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _build_properties(c: ContactInfo) -> dict:
    props: dict = {
        "email":            c.email,
        "hs_analytics_source": "OTHER_CAMPAIGNS",   # "Gmail" come sorgente custom
    }
    if c.first_name:
        props["firstname"] = c.first_name
    if c.last_name:
        props["lastname"] = c.last_name
    if c.company:
        props["company"] = c.company
    # Note: HubSpot non ha un campo nativo "source label" libero —
    # usiamo il campo "message" per il tag "Inbound Gmail"
    props["message"] = "Inbound Gmail"
    return props


def _missing_fields(existing: dict, contact: ContactInfo) -> dict:
    """Restituisce solo i campi che mancano nel record esistente."""
    p = existing.get("properties", {})
    updates = {}
    if not p.get("firstname") and contact.first_name:
        updates["firstname"] = contact.first_name
    if not p.get("lastname") and contact.last_name:
        updates["lastname"] = contact.last_name
    if not p.get("company") and contact.company:
        updates["company"] = contact.company
    return updates


# ── State persistence ─────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Core sync logic ───────────────────────────────────────────────────────────
def process_contact(hs: HubSpotClient, contact: ContactInfo) -> SyncResult:
    existing = hs.find_contact_by_email(contact.email)

    if existing:
        contact_id = existing["id"]
        updates = _missing_fields(existing, contact)
        if updates:
            hs.update_contact(contact_id, updates)
            log.info("↑  AGGIORNATO  %s  (id=%s, campi=%s)",
                     contact.email, contact_id, list(updates.keys()))
            return SyncResult(contact.email, "AGGIORNATO", contact_id)
        else:
            log.info("⊙  IGNORATO    %s  (già completo)", contact.email)
            return SyncResult(contact.email, "IGNORATO", contact_id,
                              reason="contatto già esistente e completo")
    else:
        new_id = hs.create_contact(contact)
        # Aggiunge nota con soggetto email
        if contact.source_subject:
            hs.create_note(
                new_id,
                subject="Email ricevuta via Gmail",
                body=(
                    f"Contatto acquisito da email in arrivo.\n"
                    f"Oggetto: {contact.source_subject}\n"
                    f"Dominio: {contact.domain}\n"
                    f"Tag: Inbound Gmail"
                ),
            )
        log.info("✚  CREATO      %s  (id=%s)", contact.email, new_id)
        return SyncResult(contact.email, "CREATO", new_id)


def run_sync_cycle(gmail: GmailClient, hs: HubSpotClient, state: dict) -> dict:
    """Esegue un ciclo di sincronizzazione e aggiorna lo stato."""
    results: list[SyncResult] = []
    seen_emails: set[str] = set()          # dedup dentro il ciclo corrente

    history_id = state.get("last_history_id")

    if not history_id:
        # Prima esecuzione: leggi l'historyId corrente e termina
        current_id = gmail.get_history_id()
        log.info("Prima esecuzione: salvato historyId=%s. Il prossimo ciclo processerà le nuove email.", current_id)
        state["last_history_id"] = current_id
        return state

    messages = gmail.list_new_messages(history_id)
    log.info("Trovati %d nuovi messaggi da processare.", len(messages))

    for msg in messages:
        contact = gmail.get_sender(msg["id"])
        if contact is None:
            continue
        if contact.email in seen_emails:
            continue
        seen_emails.add(contact.email)
        result = process_contact(hs, contact)
        results.append(result)

    # Aggiorna historyId
    new_history_id = gmail.get_history_id()
    state["last_history_id"] = new_history_id
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["last_run_stats"] = {
        "total":     len(results),
        "creato":    sum(1 for r in results if r.status == "CREATO"),
        "aggiornato": sum(1 for r in results if r.status == "AGGIORNATO"),
        "ignorato":  sum(1 for r in results if r.status == "IGNORATO"),
    }

    # ── Output tabellare ──────────────────────────────────────────────────
    if results:
        print("\n┌─────────────────────────────────────────────────────────────────┐")
        print(f"│ Sync completata — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}                       │")
        print("├────────────┬─────────────────────────────────┬───────────────────┤")
        print("│  Stato     │  Email contatto                 │  ID HubSpot       │")
        print("├────────────┼─────────────────────────────────┼───────────────────┤")
        for r in results:
            stato = r.status.ljust(10)
            email = r.email[:31].ljust(31)
            hid   = (r.hubspot_id or "—").ljust(17)
            print(f"│ {stato} │ {email} │ {hid} │")
        print("└────────────┴─────────────────────────────────┴───────────────────┘\n")

    return state


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    if not HUBSPOT_API_KEY:
        raise SystemExit("❌  Variabile HUBSPOT_API_KEY non impostata.")

    gmail = GmailClient()
    hs    = HubSpotClient(HUBSPOT_API_KEY)
    state = load_state()

    log.info("🚀  Gmail→HubSpot Sync avviato (intervallo=%ds)", POLL_INTERVAL_SEC)

    while True:
        try:
            state = run_sync_cycle(gmail, hs, state)
            save_state(state)
        except Exception as e:
            log.error("Errore nel ciclo di sync: %s", e, exc_info=True)

        log.info("⏳  Prossimo ciclo tra %d secondi…", POLL_INTERVAL_SEC)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
