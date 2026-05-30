"""
Gmail → HubSpot Contact Sync
Monitora Gmail in arrivo, estrae i mittenti e li sincronizza in HubSpot.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"


# ── Configurazione ─────────────────────────────────────────────────────────────

def _env_list(key: str, default: str = "") -> list[str]:
    return [v.strip().lower() for v in os.getenv(key, default).split(",") if v.strip()]


HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
GMAIL_BATCH_SIZE = int(os.getenv("GMAIL_BATCH_SIZE", "20"))
IGNORED_DOMAINS = set(_env_list("IGNORED_DOMAINS", ""))
STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))


# ── Modelli dati ───────────────────────────────────────────────────────────────

@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""
    subject: str = ""
    received_at: str = ""


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    contact_id: Optional[str] = None
    reason: str = ""


@dataclass
class SyncState:
    last_history_id: str = ""
    last_sync_ts: int = 0          # unix timestamp in ms dell'ultima email processata
    processed_message_ids: list = field(default_factory=list)

    def save(self, path: Path) -> None:
        # Tieni solo gli ultimi 5000 ID per non far crescere il file infinitamente
        self.processed_message_ids = self.processed_message_ids[-5000:]
        path.write_text(json.dumps(self.__dict__, indent=2))

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        if path.exists():
            data = json.loads(path.read_text())
            return cls(**data)
        return cls()


# ── Parsing mittente ───────────────────────────────────────────────────────────

def _extract_name_parts(display_name: str) -> tuple[str, str]:
    """Ritorna (first_name, last_name) da una stringa 'Nome Cognome'."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    """Ricava un nome azienda approssimativo dal dominio (es. acme.com → Acme)."""
    name = domain.split(".")[0]
    return name.capitalize() if name else ""


_PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "live.com", "aol.com", "protonmail.com", "libero.it", "alice.it",
    "tiscali.it", "virgilio.it", "tin.it", "fastwebnet.it", "me.com",
}


def parse_sender(from_header: str, subject: str = "", date_header: str = "") -> Optional[SenderInfo]:
    """Estrae i dati del mittente dall'header From di Gmail."""
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()

    if not email_addr or "@" not in email_addr:
        return None

    domain = email_addr.split("@")[1]

    if domain in IGNORED_DOMAINS:
        return None

    # Filtra indirizzi tipicamente automatici
    local_part = email_addr.split("@")[0]
    if any(kw in local_part for kw in ("noreply", "no-reply", "donotreply", "mailer", "bounce", "daemon")):
        return None

    first_name, last_name = _extract_name_parts(display_name)

    # Azienda solo se il dominio non è personale
    company = "" if domain in _PERSONAL_DOMAINS else _company_from_domain(domain)

    received_at = ""
    if date_header:
        try:
            dt = parsedate_to_datetime(date_header)
            received_at = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass

    return SenderInfo(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
        subject=subject,
        received_at=received_at,
    )


# ── Client Gmail ───────────────────────────────────────────────────────────────

class GmailClient:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.json"):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = self._authenticate()

    def _authenticate(self):
        # Import lazy per evitare conflitti cffi in ambienti senza la libreria completa
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if Path(self._token_file).exists():
            creds = Credentials.from_authorized_user_file(self._token_file, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not Path(self._credentials_file).exists():
                    raise FileNotFoundError(
                        f"File '{self._credentials_file}' non trovato.\n"
                        "Scaricalo da Google Cloud Console → API & Servizi → Credenziali → OAuth 2.0."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(self._credentials_file, GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
            Path(self._token_file).write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_inbox_messages(self, after_ts_ms: int = 0, max_results: int = 20) -> list[dict]:
        """Ritorna i messaggi dalla INBOX più recenti di after_ts_ms (epoch ms)."""
        query = "in:inbox -in:spam -in:trash"
        if after_ts_ms:
            after_sec = int(after_ts_ms / 1000)
            query += f" after:{after_sec}"

        try:
            resp = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
        except Exception as e:
            log.error("Errore Gmail API: %s", e)
            return []

        messages = resp.get("messages", [])
        return messages

    def get_message_headers(self, message_id: str) -> dict:
        """Ritorna gli header essenziali di un messaggio."""
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except Exception as e:
            log.error("Errore lettura messaggio %s: %s", message_id, e)
            return {}

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        headers["_internal_date_ms"] = int(msg.get("internalDate", 0))
        return headers


# ── Client HubSpot ─────────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self, token: str):
        if not token:
            raise ValueError(
                "HUBSPOT_TOKEN mancante. Aggiungilo al file .env.\n"
                "Crea un Private App su HubSpot → Impostazioni → Integrazioni → App private."
            )
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(f"{HUBSPOT_BASE}{path}", headers=self._headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        resp = requests.post(f"{HUBSPOT_BASE}{path}", headers=self._headers, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, body: dict) -> dict:
        resp = requests.patch(f"{HUBSPOT_BASE}{path}", headers=self._headers, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Cerca un contatto HubSpot per email. Ritorna None se non trovato."""
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_status", "lead_source"],
            "limit": 1,
        }
        try:
            data = self._post("/crm/v3/objects/contacts/search", body)
            results = data.get("results", [])
            return results[0] if results else None
        except requests.HTTPError as e:
            log.error("Errore ricerca contatto %s: %s", email, e)
            return None

    def create_contact(self, sender: SenderInfo) -> Optional[str]:
        """Crea un nuovo contatto HubSpot. Ritorna l'ID creato."""
        props = self._build_properties(sender)
        body = {"properties": props}
        try:
            data = self._post("/crm/v3/objects/contacts", body)
            contact_id = data["id"]
            self._add_note(contact_id, sender)
            return contact_id
        except requests.HTTPError as e:
            log.error("Errore creazione contatto %s: %s", sender.email, e)
            return None

    def update_contact(self, contact_id: str, sender: SenderInfo, existing: dict) -> bool:
        """Aggiorna i campi vuoti di un contatto esistente. Ritorna True se ha modificato qualcosa."""
        existing_props = existing.get("properties", {})
        updates = {}

        # Aggiorna solo i campi mancanti/vuoti
        if not existing_props.get("firstname") and sender.first_name:
            updates["firstname"] = sender.first_name
        if not existing_props.get("lastname") and sender.last_name:
            updates["lastname"] = sender.last_name
        if not existing_props.get("company") and sender.company:
            updates["company"] = sender.company

        # Imposta sempre la fonte se non presente
        if not existing_props.get("lead_source"):
            updates["lead_source"] = CONTACT_SOURCE

        if not updates:
            return False

        try:
            self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": updates})
            self._add_note(contact_id, sender)
            return True
        except requests.HTTPError as e:
            log.error("Errore aggiornamento contatto %s: %s", contact_id, e)
            return False

    def _build_properties(self, sender: SenderInfo) -> dict:
        props: dict[str, str] = {"email": sender.email, "lead_source": CONTACT_SOURCE}
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company
        return props

    def _add_note(self, contact_id: str, sender: SenderInfo) -> None:
        """Aggiunge una nota-attività al contatto con dettagli dell'email ricevuta."""
        body_lines = [
            f"📧 Email ricevuta via Gmail",
            f"Da: {sender.email}",
        ]
        if sender.subject:
            body_lines.append(f"Oggetto: {sender.subject}")
        if sender.received_at:
            body_lines.append(f"Ricevuta: {sender.received_at}")
        body_lines.append(f"Tag: {CONTACT_TAG}")

        note_body = {
            "properties": {
                "hs_note_body": "\n".join(body_lines),
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                }
            ],
        }
        try:
            self._post("/crm/v3/objects/notes", note_body)
        except requests.HTTPError as e:
            # La nota è opzionale, non bloccare il flusso
            log.warning("Impossibile aggiungere nota al contatto %s: %s", contact_id, e)


# ── Motore di sincronizzazione ─────────────────────────────────────────────────

class GmailHubSpotSync:
    def __init__(self):
        self.gmail = GmailClient()
        self.hubspot = HubSpotClient(HUBSPOT_TOKEN)
        self.state = SyncState.load(STATE_FILE)
        log.info("Sync avviato. Ultimo timestamp: %s", self.state.last_sync_ts or "nessuno")

    def run_once(self) -> list[SyncResult]:
        """Esegue un ciclo di sincronizzazione. Ritorna i risultati."""
        messages = self.gmail.get_inbox_messages(
            after_ts_ms=self.state.last_sync_ts,
            max_results=GMAIL_BATCH_SIZE,
        )

        if not messages:
            log.info("Nessuna nuova email.")
            return []

        results: list[SyncResult] = []
        max_ts = self.state.last_sync_ts

        for msg in messages:
            msg_id = msg["id"]

            if msg_id in self.state.processed_message_ids:
                continue

            headers = self.gmail.get_message_headers(msg_id)
            if not headers:
                continue

            internal_date = headers.get("_internal_date_ms", 0)
            max_ts = max(max_ts, internal_date)

            sender = parse_sender(
                from_header=headers.get("From", ""),
                subject=headers.get("Subject", ""),
                date_header=headers.get("Date", ""),
            )

            self.state.processed_message_ids.append(msg_id)

            if sender is None:
                results.append(SyncResult(status="Ignorato", email=headers.get("From", "?"),
                                           reason="mittente non valido o da ignorare"))
                continue

            result = self._sync_contact(sender)
            results.append(result)

        # Aggiorna stato
        if max_ts > self.state.last_sync_ts:
            self.state.last_sync_ts = max_ts
        self.state.save(STATE_FILE)

        return results

    def _sync_contact(self, sender: SenderInfo) -> SyncResult:
        existing = self.hubspot.find_contact_by_email(sender.email)

        if existing is None:
            contact_id = self.hubspot.create_contact(sender)
            if contact_id:
                return SyncResult(status="Creato", email=sender.email, contact_id=contact_id)
            return SyncResult(status="Ignorato", email=sender.email, reason="errore creazione HubSpot")

        contact_id = existing["id"]
        updated = self.hubspot.update_contact(contact_id, sender, existing)
        status = "Aggiornato" if updated else "Ignorato"
        reason = "" if updated else "nessun campo da aggiornare"
        return SyncResult(status=status, email=sender.email, contact_id=contact_id, reason=reason)

    def run_loop(self) -> None:
        """Loop continuo di polling."""
        log.info("Avvio monitoraggio Gmail → HubSpot (intervallo: %ds)", POLL_INTERVAL)
        while True:
            try:
                results = self.run_once()
                _print_results(results)
            except KeyboardInterrupt:
                log.info("Interruzione manuale. Uscita.")
                break
            except Exception as e:
                log.error("Errore inatteso nel ciclo: %s", e, exc_info=True)

            time.sleep(POLL_INTERVAL)


# ── Output ─────────────────────────────────────────────────────────────────────

_STATUS_ICON = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️"}


def _print_results(results: list[SyncResult]) -> None:
    if not results:
        return
    print(f"\n{'─'*60}")
    print(f"  Ciclo completato: {len(results)} email processate")
    print(f"{'─'*60}")
    for r in results:
        icon = _STATUS_ICON.get(r.status, "•")
        cid = f"ID: {r.contact_id}" if r.contact_id else r.reason
        print(f"  {icon} [{r.status:10s}] {r.email:<40s} {cid}")
    print(f"{'─'*60}\n")


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sync = GmailHubSpotSync()
    sync.run_loop()
