"""Client Gmail con polling basato su History API per efficienza."""

from __future__ import annotations

import logging
import os
from typing import Iterator, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class HistoryExpiredError(Exception):
    """Sollevata quando il historyId è troppo vecchio (> 7 giorni) e va rifatto full sync."""


class GmailClient:
    def __init__(self, credentials_path: str, token_path: str):
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._svc = None

    # ------------------------------------------------------------------
    # Autenticazione
    # ------------------------------------------------------------------

    def _service(self):
        if self._svc:
            return self._svc

        creds: Optional[Credentials] = None
        if os.path.exists(self._token_path):
            creds = Credentials.from_authorized_user_file(self._token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError:
                    creds = None

            if not creds:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(self._token_path, "w") as f:
                f.write(creds.to_json())

        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._svc

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def get_current_history_id(self) -> str:
        """Restituisce il historyId corrente del profilo (usato al primo avvio)."""
        profile = self._service().users().getProfile(userId="me").execute()
        return profile["historyId"]

    def list_new_inbox_messages(self, start_history_id: str) -> tuple[list[str], str]:
        """
        Usa la History API per ottenere i messaggi aggiunti all'inbox dopo start_history_id.

        Restituisce (lista_message_id, nuovo_history_id).
        Solleva HistoryExpiredError se il historyId è scaduto.
        """
        message_ids: list[str] = []
        page_token: Optional[str] = None
        latest_history_id = start_history_id

        while True:
            kwargs: dict = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "labelId": "INBOX",
                "historyTypes": ["messageAdded"],
            }
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                result = self._service().users().history().list(**kwargs).execute()
            except HttpError as exc:
                if exc.status_code == 404:
                    raise HistoryExpiredError(f"historyId {start_history_id} scaduto") from exc
                raise

            for record in result.get("history", []):
                latest_history_id = record.get("id", latest_history_id)
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    if "INBOX" in msg.get("labelIds", []) and "SENT" not in msg.get("labelIds", []):
                        message_ids.append(msg["id"])

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return message_ids, latest_history_id

    def list_inbox_messages_since_days(self, days: int = 7, max_results: int = 200) -> tuple[list[str], str]:
        """Full scan dell'inbox (usato al primo avvio o dopo historyId scaduto)."""
        query = f"in:inbox -from:me newer_than:{days}d"
        message_ids: list[str] = []
        page_token: Optional[str] = None

        while len(message_ids) < max_results:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": min(50, max_results - len(message_ids))}
            if page_token:
                kwargs["pageToken"] = page_token

            result = self._service().users().messages().list(**kwargs).execute()
            for msg in result.get("messages", []):
                message_ids.append(msg["id"])

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        history_id = self.get_current_history_id()
        return message_ids, history_id

    def get_message_metadata(self, message_id: str) -> dict:
        """Recupera solo gli header necessari (From, Subject, Date) per efficienza."""
        return (
            self._service()
            .users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )

    # ------------------------------------------------------------------
    # Helper header
    # ------------------------------------------------------------------

    @staticmethod
    def header(message: dict, name: str) -> Optional[str]:
        name_lower = name.lower()
        for h in message.get("payload", {}).get("headers", []):
            if h["name"].lower() == name_lower:
                return h["value"]
        return None

    @staticmethod
    def internal_date_epoch(message: dict) -> int:
        """internalDate è in millisecondi → convertiamo in secondi."""
        return int(message.get("internalDate", 0)) // 1000
