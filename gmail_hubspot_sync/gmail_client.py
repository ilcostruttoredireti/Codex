"""
gmail_client.py — Wrapper per l'API Gmail.

Funzionalità:
- Autenticazione OAuth 2.0 con refresh automatico del token
- Ricerca messaggi non processati (senza label 'HubSpot-Synced')
- Parsing header mittente (From, Reply-To)
- Applicazione label ai messaggi processati
- Creazione automatica della label se non esiste
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import config
from email_parser import SenderInfo, parse_from_header
from logger import get_logger

# Scope minimo necessario
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

log = get_logger("gmail", config.log_file)


@dataclass
class SenderInfo:
    """Dati estratti dall'header From di un'email."""

    email: str
    first_name: str
    last_name: str
    full_name: str
    company_domain: str
    message_id: str
    subject: str
    date: str


class GmailClient:
    def __init__(self) -> None:
        self._service = None
        self._processed_label_id: Optional[str] = None

    # ── Autenticazione ─────────────────────────────────────────────────────

    def authenticate(self) -> None:
        """Esegue il flusso OAuth; salva/ricarica il token da file."""
        creds: Optional[Credentials] = None
        token_path = config.gmail_token_file

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                log.info("Rinnovo del token Gmail...")
                creds.refresh(Request())
            else:
                log.info("Avvio del flusso OAuth Gmail (browser)...")
                creds_file = config.gmail_credentials_file

                # Fallback: costruisce credentials.json da variabili d'ambiente
                if not creds_file.exists():
                    creds_file = self._build_credentials_file()

                flow = InstalledAppFlow.from_client_secrets_file(
                    str(creds_file), SCOPES
                )
                creds = flow.run_local_server(port=0)

            # Salva per i run successivi
            token_path.write_text(creds.to_json(), encoding="utf-8")

        self._service = build("gmail", "v1", credentials=creds)
        log.info("✅  Autenticazione Gmail completata.")

    def _build_credentials_file(self) -> Path:
        """Crea credentials.json temporaneo dalle env vars."""
        if not config.gmail_client_id or not config.gmail_client_secret:
            raise RuntimeError(
                "Né credentials.json né GMAIL_CLIENT_ID/SECRET sono configurati."
            )
        data = {
            "installed": {
                "client_id": config.gmail_client_id,
                "client_secret": config.gmail_client_secret,
                "redirect_uris": ["http://localhost"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        path = config.gmail_credentials_file
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    # ── Labels ─────────────────────────────────────────────────────────────

    def _get_or_create_label(self) -> str:
        """Restituisce l'id della label; la crea se non esiste."""
        if self._processed_label_id:
            return self._processed_label_id

        result = self._service.users().labels().list(userId="me").execute()
        for lbl in result.get("labels", []):
            if lbl["name"].lower() == config.processed_label.lower():
                self._processed_label_id = lbl["id"]
                return self._processed_label_id

        # Crea la label
        created = (
            self._service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": config.processed_label,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        self._processed_label_id = created["id"]
        log.info(f"Label Gmail creata: '{config.processed_label}' (id={self._processed_label_id})")
        return self._processed_label_id

    def mark_as_processed(self, message_id: str) -> None:
        """Applica la label 'HubSpot-Synced' al messaggio."""
        label_id = self._get_or_create_label()
        try:
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except HttpError as exc:
            log.warning(f"Impossibile applicare la label al messaggio {message_id}: {exc}")

    # ── Fetch messaggi ─────────────────────────────────────────────────────

    def fetch_new_messages(self) -> list[SenderInfo]:
        """
        Recupera tutti i messaggi in INBOX non ancora etichettati.
        Ritorna una lista di SenderInfo pronti per HubSpot.
        """
        label_id = self._get_or_create_label()
        query = f"in:inbox -label:{config.processed_label}"

        senders: list[SenderInfo] = []
        next_page_token: Optional[str] = None

        while True:
            try:
                kwargs: dict = {
                    "userId": "me",
                    "q": query,
                    "maxResults": 100,
                }
                if next_page_token:
                    kwargs["pageToken"] = next_page_token

                response = self._service.users().messages().list(**kwargs).execute()
                messages = response.get("messages", [])

                for msg_stub in messages:
                    info = self._parse_message(msg_stub["id"])
                    if info:
                        senders.append(info)

                next_page_token = response.get("nextPageToken")
                if not next_page_token:
                    break

            except HttpError as exc:
                log.error(f"Errore nella lettura dei messaggi Gmail: {exc}")
                break

        log.info(f"📬  Trovati {len(senders)} nuovi messaggi da processare.")
        return senders

    # ── Parsing ────────────────────────────────────────────────────────────

    def _parse_message(self, message_id: str) -> Optional[SenderInfo]:
        """Recupera l'header e torna un SenderInfo; None se da ignorare."""
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Reply-To", "Subject", "Date"])
                .execute()
            )
        except HttpError as exc:
            log.warning(f"Impossibile leggere il messaggio {message_id}: {exc}")
            return None

        headers: dict[str, str] = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        from_header = headers.get("Reply-To") or headers.get("From", "")
        if not from_header:
            return None

        info = parse_from_header(
            from_header=from_header,
            message_id=message_id,
            subject=headers.get("Subject", ""),
            date=headers.get("Date", ""),
            ignore_domains=config.ignore_domains,
        )
        if info is None:
            log.debug(f"Messaggio {message_id} ignorato (dominio filtrato o no email).")
        return info
