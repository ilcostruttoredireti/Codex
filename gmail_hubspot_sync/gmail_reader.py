"""
Lettura email Gmail tramite Gmail API (OAuth2).
Mantiene in memoria l'ultimo historyId processato per rilevare solo i nuovi messaggi.
"""

import base64
import json
import logging
import os
from pathlib import Path
from typing import Generator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_STATE_FILE = Path(".gmail_state.json")


class GmailReader:
    """Wrapper attorno alla Gmail API per il polling incrementale dei messaggi."""

    def __init__(self, credentials_file: str, token_file: str):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None
        self._last_history_id: Optional[str] = None
        self._load_state()

    # ------------------------------------------------------------------
    # Autenticazione
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Avvia il flusso OAuth2 e costruisce il servizio Gmail."""
        creds = self._load_credentials()
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        logger.info("Gmail autenticato con successo.")

    def _load_credentials(self) -> Credentials:
        creds = None

        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, _SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "w") as f:
                f.write(creds.to_json())

        return creds

    # ------------------------------------------------------------------
    # Polling messaggi
    # ------------------------------------------------------------------

    def fetch_new_messages(self, label: str = "INBOX") -> Generator[dict, None, None]:
        """
        Restituisce i nuovi messaggi ricevuti dall'ultima chiamata.
        Al primo avvio restituisce i messaggi dell'ultima ora.
        """
        if self._service is None:
            raise RuntimeError("Chiama authenticate() prima di fetch_new_messages().")

        try:
            if self._last_history_id:
                yield from self._fetch_via_history(label)
            else:
                yield from self._fetch_recent(label)
        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId scaduto: riparte da zero
                logger.warning("historyId non più valido, reset dello stato.")
                self._last_history_id = None
                self._save_state()
                yield from self._fetch_recent(label)
            else:
                raise

    def _fetch_recent(self, label: str) -> Generator[dict, None, None]:
        """Scarica i messaggi recenti (ultima ora) e imposta l'historyId di partenza."""
        result = (
            self._service.users()
            .messages()
            .list(userId="me", labelIds=[label], maxResults=50, q="newer_than:1h")
            .execute()
        )
        messages = result.get("messages", [])
        profile = self._service.users().getProfile(userId="me").execute()
        self._last_history_id = profile.get("historyId")
        self._save_state()

        for msg_stub in messages:
            msg = self._get_full_message(msg_stub["id"])
            if msg:
                yield msg

    def _fetch_via_history(self, label: str) -> Generator[dict, None, None]:
        """Usa la History API per ottenere solo i messaggi aggiunti dopo l'ultimo historyId."""
        result = (
            self._service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=self._last_history_id,
                historyTypes=["messageAdded"],
                labelId=label,
            )
            .execute()
        )

        new_history_id = result.get("historyId", self._last_history_id)
        history_items = result.get("history", [])

        seen_ids: set[str] = set()
        for item in history_items:
            for added in item.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                if msg_id not in seen_ids:
                    seen_ids.add(msg_id)
                    msg = self._get_full_message(msg_id)
                    if msg:
                        yield msg

        self._last_history_id = new_history_id
        self._save_state()

    def _get_full_message(self, msg_id: str) -> Optional[dict]:
        try:
            return (
                self._service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError as exc:
            logger.warning("Impossibile recuperare il messaggio %s: %s", msg_id, exc)
            return None

    # ------------------------------------------------------------------
    # Utilità header
    # ------------------------------------------------------------------

    @staticmethod
    def extract_header(message: dict, name: str) -> str:
        """Restituisce il valore di un header dal messaggio Gmail."""
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    # ------------------------------------------------------------------
    # Persistenza stato
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if _STATE_FILE.exists():
            try:
                data = json.loads(_STATE_FILE.read_text())
                self._last_history_id = data.get("last_history_id")
            except (json.JSONDecodeError, OSError):
                self._last_history_id = None

    def _save_state(self) -> None:
        try:
            _STATE_FILE.write_text(
                json.dumps({"last_history_id": self._last_history_id})
            )
        except OSError as exc:
            logger.warning("Impossibile salvare lo stato: %s", exc)
