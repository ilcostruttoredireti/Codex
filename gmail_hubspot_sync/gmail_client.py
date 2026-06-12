import logging
import os
from typing import Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Solo lettura — non modifica la casella
_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = self._authenticate()

    # ── Autenticazione ────────────────────────────────────────────────────────

    def _authenticate(self):
        creds: Optional[Credentials] = None

        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self._credentials_file):
                    raise FileNotFoundError(
                        f"File credenziali Gmail non trovato: {self._credentials_file}\n"
                        "Scaricalo da Google Cloud Console → APIs & Services → "
                        "Credentials → OAuth 2.0 Client IDs → Download JSON"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, _SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(self._token_file, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ── API pubblica ──────────────────────────────────────────────────────────

    def get_profile(self) -> Dict:
        return self._service.users().getProfile(userId="me").execute()

    def get_current_history_id(self) -> str:
        return str(self.get_profile()["historyId"])

    def get_messages_since_history(
        self, start_history_id: str
    ) -> Tuple[Optional[List[str]], str]:
        """
        Usa l'API Gmail History per recuperare solo i nuovi messaggi in arrivo.
        Restituisce (lista_id, history_id_aggiornato).
        Restituisce (None, history_id_corrente) se l'history ID è scaduto (>7 giorni).
        """
        message_ids: List[str] = []
        latest_history_id = start_history_id
        page_token: Optional[str] = None

        try:
            while True:
                kwargs = {
                    "userId": "me",
                    "startHistoryId": start_history_id,
                    "historyTypes": ["messageAdded"],
                    "labelId": "INBOX",
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                resp = self._service.users().history().list(**kwargs).execute()
                latest_history_id = str(resp.get("historyId", latest_history_id))

                for item in resp.get("history", []):
                    for added in item.get("messagesAdded", []):
                        msg = added.get("message", {})
                        labels = msg.get("labelIds", [])
                        # Solo email in arrivo (no SENT, no DRAFT, no SPAM)
                        if "INBOX" in labels and "SENT" not in labels:
                            message_ids.append(msg["id"])

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as exc:
            if exc.resp.status == 404:
                # History ID scaduto (>7 giorni senza esecuzioni)
                logger.warning(
                    "History ID scaduto — reset a messaggi recenti della inbox"
                )
                return None, self.get_current_history_id()
            raise

        return message_ids, latest_history_id

    def get_recent_inbox_messages(
        self, max_results: int = 50
    ) -> Tuple[List[str], str]:
        """
        Primo avvio: recupera gli ultimi N messaggi della inbox
        e restituisce anche l'history ID corrente.
        """
        resp = (
            self._service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        ids = [m["id"] for m in resp.get("messages", [])]
        return ids, self.get_current_history_id()

    def get_message_headers(self, message_id: str) -> Optional[Dict]:
        """
        Recupera gli header rilevanti (From, Subject, Date) di un messaggio.
        Usa format=metadata per minimizzare il payload.
        """
        try:
            msg = (
                self._service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers: Dict[str, str] = {
                h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            return {
                "id": message_id,
                "thread_id": msg.get("threadId", ""),
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            }
        except HttpError as exc:
            logger.error(f"Errore recupero messaggio {message_id}: {exc}")
            return None
