#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitora la Gmail inbox per nuove email e sincronizza i mittenti come
contatti in HubSpot (crea se non esiste, aggiorna i campi mancanti).

Setup rapido:
  1. cp .env.example .env  → inserisci HUBSPOT_ACCESS_TOKEN
  2. Scarica credentials.json da Google Cloud Console (OAuth2, tipo "App desktop")
  3. pip install -r requirements.txt
  4. python sync.py          # modalità continua (polling ogni POLL_INTERVAL_SECONDS)
  5. python sync.py --once   # processa inbox una volta sola (utile per cron)

Al primo avvio si apre automaticamente il browser per autorizzare Gmail.
"""

import argparse
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configurazione ────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
SYNCED_LABEL = "HubSpot-Synced"

# Domini email personali: non si deriva il nome azienda
PERSONAL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "me.com", "libero.it", "tiscali.it",
    "virgilio.it", "alice.it", "tin.it",
})

# Mittenti automatici da ignorare
SKIP_RE = re.compile(
    r"(noreply|no-reply|mailer-daemon|postmaster|bounce|donotreply|"
    r"do-not-reply|notifications?|newsletter|automated|alerts?|"
    r"unsubscribe|billing|invoice|receipt|confirm|verify|security|"
    r"updates?@|info@|support@|hello@|team@|admin@)",
    re.IGNORECASE,
)

# ── Modelli dati ──────────────────────────────────────────────────────────────


@dataclass
class SenderContact:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None


@dataclass
class SyncResult:
    status: str  # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: Optional[str] = None

    def __str__(self) -> str:
        icon = {"Creato": "✚", "Aggiornato": "↺", "Ignorato": "─"}.get(self.status, " ")
        return (
            f"{icon} {self.status:10s} | "
            f"{self.email:45s} | "
            f"ID: {self.hubspot_id or 'N/A'}"
        )


# ── Parser mittente ───────────────────────────────────────────────────────────


def parse_sender(from_header: str) -> Optional[SenderContact]:
    """Estrae email, nome e azienda dall'header From. Ritorna None se da ignorare."""
    display_name, email_addr = parseaddr(from_header)
    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()

    if SKIP_RE.search(email_addr) or SKIP_RE.search(display_name):
        return None

    domain = email_addr.split("@", 1)[1]

    # Estrai nome e cognome dal display name
    first_name = last_name = None
    name = re.sub(r'[<>"\']', "", display_name).strip()
    if name:
        parts = name.split()
        if len(parts) >= 2:
            first_name = parts[0].title()
            last_name = " ".join(parts[1:]).title()
        elif len(parts) == 1:
            first_name = parts[0].title()

    # Deriva il nome azienda dal dominio (solo per domini business)
    company = None
    if domain not in PERSONAL_DOMAINS:
        raw = domain.split(".")[0]
        company = re.sub(r"[-_]", " ", raw).title()

    return SenderContact(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )


# ── Client Gmail ──────────────────────────────────────────────────────────────


class GmailClient:
    def __init__(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

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
            with open(GMAIL_TOKEN_FILE, "w") as fh:
                fh.write(creds.to_json())

        self._svc = build("gmail", "v1", credentials=creds)
        profile = self._svc.users().getProfile(userId="me").execute()
        self.user_email: str = profile["emailAddress"].lower()
        self._label_id = self._ensure_label(SYNCED_LABEL)
        log.info(f"Gmail connesso: {self.user_email}")

    def _ensure_label(self, name: str) -> str:
        """Recupera o crea l'etichetta Gmail usata per tracciare i messaggi già processati."""
        labels = self._svc.users().labels().list(userId="me").execute().get("labels", [])
        for lbl in labels:
            if lbl["name"] == name:
                return lbl["id"]
        return self._svc.users().labels().create(
            userId="me", body={"name": name}
        ).execute()["id"]

    def get_unsynced_messages(self, max_results: int = 50) -> list[dict]:
        """Restituisce i messaggi in inbox non ancora processati."""
        resp = self._svc.users().messages().list(
            userId="me",
            q=f"in:inbox -label:{SYNCED_LABEL}",
            maxResults=max_results,
        ).execute()
        return resp.get("messages", [])

    def get_from_header(self, message_id: str) -> str:
        """Recupera solo l'header From di un messaggio (chiamata leggera)."""
        msg = self._svc.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From"],
        ).execute()
        for h in msg.get("payload", {}).get("headers", []):
            if h["name"] == "From":
                return h["value"]
        return ""

    def mark_synced(self, message_id: str):
        """Applica l'etichetta HubSpot-Synced al messaggio."""
        self._svc.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [self._label_id]},
        ).execute()


# ── Client HubSpot ────────────────────────────────────────────────────────────


class HubSpotClient:
    def __init__(self, access_token: str):
        import hubspot
        from hubspot.crm.contacts import (
            SimplePublicObjectInputForCreate,
            SimplePublicObjectInput,
            PublicObjectSearchRequest,
            Filter,
            FilterGroup,
        )

        self._client = hubspot.Client.create(access_token=access_token)
        self._Create = SimplePublicObjectInputForCreate
        self._Update = SimplePublicObjectInput
        self._SearchReq = PublicObjectSearchRequest
        self._Filter = Filter
        self._FilterGroup = FilterGroup
        log.info("HubSpot client inizializzato")

    def find_by_email(self, email: str):
        """Cerca un contatto per email. Ritorna l'oggetto HubSpot o None."""
        try:
            req = self._SearchReq(
                filter_groups=[
                    self._FilterGroup(filters=[
                        self._Filter(property_name="email", operator="EQ", value=email)
                    ])
                ],
                properties=["email", "firstname", "lastname", "company"],
                limit=1,
            )
            resp = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=req
            )
            return resp.results[0] if resp.total > 0 else None
        except Exception as exc:
            log.error(f"HubSpot ricerca fallita ({email}): {exc}")
            return None

    def create_contact(self, sender: SenderContact) -> Optional[str]:
        """Crea un nuovo contatto HubSpot. Ritorna l'ID o None in caso di errore."""
        props: dict[str, str] = {
            "email": sender.email,
            "hs_lead_source": "OTHER",
        }
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company

        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=self._Create(properties=props)
            )
            contact_id = str(result.id)
            self._add_gmail_note(contact_id)
            return contact_id
        except Exception as exc:
            log.error(f"HubSpot creazione fallita ({sender.email}): {exc}")
            return None

    def update_missing_fields(
        self, contact_id: str, existing_props: dict, sender: SenderContact
    ) -> bool:
        """Aggiorna solo i campi vuoti nel contatto esistente. Ritorna True se c'è stato un aggiornamento."""
        updates: dict[str, str] = {}
        if not existing_props.get("firstname") and sender.first_name:
            updates["firstname"] = sender.first_name
        if not existing_props.get("lastname") and sender.last_name:
            updates["lastname"] = sender.last_name
        if not existing_props.get("company") and sender.company:
            updates["company"] = sender.company

        if not updates:
            return False
        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=self._Update(properties=updates),
            )
            return True
        except Exception as exc:
            log.error(f"HubSpot aggiornamento fallito ({contact_id}): {exc}")
            return False

    def _add_gmail_note(self, contact_id: str):
        """Aggiunge una nota al contatto con la fonte Gmail (opzionale, fallisce silenziosamente)."""
        try:
            from hubspot.crm.objects.notes import (
                SimplePublicObjectInputForCreate as NoteInput,
            )

            note_body = (
                "Contatto acquisito automaticamente da Gmail.\n"
                "Fonte: Inbound Gmail\n"
                f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
            self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteInput(
                    properties={
                        "hs_note_body": note_body,
                        "hs_timestamp": str(int(datetime.now().timestamp() * 1000)),
                    },
                    associations=[{
                        "to": {"id": contact_id},
                        "types": [{
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,
                        }],
                    }],
                )
            )
        except Exception as exc:
            # La nota è opzionale: logghiamo solo in debug
            log.debug(f"Nota Gmail non aggiunta al contatto {contact_id}: {exc}")


# ── Logica di sincronizzazione ────────────────────────────────────────────────


def sync_contact(sender: SenderContact, hs: HubSpotClient) -> SyncResult:
    existing = hs.find_by_email(sender.email)

    if existing is None:
        contact_id = hs.create_contact(sender)
        status = "Creato" if contact_id else "Ignorato"
        return SyncResult(status, sender.email, contact_id)

    props = existing.properties or {}
    updated = hs.update_missing_fields(str(existing.id), props, sender)
    status = "Aggiornato" if updated else "Ignorato"
    return SyncResult(status, sender.email, str(existing.id))


def run_once(gmail: GmailClient, hs: HubSpotClient) -> list[SyncResult]:
    """Processa tutti i messaggi non ancora sincronizzati."""
    messages = gmail.get_unsynced_messages()
    if not messages:
        log.info("Nessun nuovo messaggio da processare.")
        return []

    log.info(f"Messaggi trovati: {len(messages)}")
    results: list[SyncResult] = []

    for msg in messages:
        try:
            from_hdr = gmail.get_from_header(msg["id"])
            sender = parse_sender(from_hdr)

            # Ignora se non parsabile, automatico, o mittente = se stessi
            if sender is None or sender.email == gmail.user_email:
                gmail.mark_synced(msg["id"])
                continue

            result = sync_contact(sender, hs)
            results.append(result)
            log.info(str(result))
            gmail.mark_synced(msg["id"])

        except Exception as exc:
            log.error(f"Errore nel processare messaggio {msg['id']}: {exc}")

    return results


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Sincronizza contatti Gmail → HubSpot"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Processa la inbox una volta e termina (ideale per cron)",
    )
    args = parser.parse_args()

    if not HUBSPOT_ACCESS_TOKEN:
        raise SystemExit(
            "HUBSPOT_ACCESS_TOKEN non impostato.\n"
            "Copia .env.example in .env e inserisci il token della tua app privata HubSpot."
        )
    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        raise SystemExit(
            f"File credenziali Gmail non trovato: {GMAIL_CREDENTIALS_FILE}\n"
            "Scaricalo da: Google Cloud Console → API e servizi → Credenziali → "
            "Crea credenziali → ID client OAuth 2.0 (tipo: App desktop)"
        )

    gmail = GmailClient()
    hs = HubSpotClient(HUBSPOT_ACCESS_TOKEN)

    if args.once:
        results = run_once(gmail, hs)
        log.info(f"Fine. Contatti processati: {len(results)}")
        return

    log.info(f"Monitoraggio avviato — polling ogni {POLL_INTERVAL}s. Ctrl+C per fermare.")
    log.info("─" * 72)
    while True:
        try:
            run_once(gmail, hs)
        except KeyboardInterrupt:
            log.info("Interruzione. Arresto.")
            break
        except Exception as exc:
            log.error(f"Errore nel ciclo principale: {exc}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
