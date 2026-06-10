import logging
import os
from dataclasses import dataclass

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    message_id: str
    from_header: str
    subject: str
    date: str
    thread_id: str


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str, scopes: list[str]):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._scopes = scopes
        self._service = None

    def authenticate(self) -> None:
        """Autentica con Gmail tramite OAuth2. Apre browser al primo avvio."""
        creds = None

        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, self._scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self._credentials_file):
                    raise FileNotFoundError(
                        f"File credenziali Gmail non trovato: {self._credentials_file}\n"
                        "Scarica credentials.json da Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, self._scopes
                )
                creds = flow.run_local_server(port=0)

            with open(self._token_file, "w") as f:
                f.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Autenticazione Gmail completata.")

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    def get_or_create_label(self, label_name: str) -> str:
        """Restituisce l'ID della label, creandola se non esiste."""
        labels = self._service.users().labels().list(userId="me").execute()
        for lbl in labels.get("labels", []):
            if lbl["name"] == label_name:
                return lbl["id"]

        created = self._service.users().labels().create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelHide",
                "messageListVisibility": "hide",
            },
        ).execute()
        logger.info(f"Label Gmail creata: '{label_name}' (ID: {created['id']})")
        return created["id"]

    def mark_as_processed(self, message_id: str, processed_label_id: str) -> None:
        """Aggiunge la label 'HubSpot-Processed' al messaggio."""
        try:
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [processed_label_id]},
            ).execute()
        except HttpError as e:
            logger.warning(f"Impossibile aggiungere label a {message_id}: {e}")

    # ------------------------------------------------------------------
    # Fetch messages
    # ------------------------------------------------------------------

    def get_unprocessed_inbox_messages(
        self, processed_label_id: str, max_results: int = 50
    ) -> list[EmailMessage]:
        """
        Restituisce i messaggi in arrivo non ancora processati.
        Esclude: messaggi inviati da sé, draft, spam, email già marcate.
        """
        query = f"in:inbox -from:me -label:{processed_label_id}"

        try:
            result = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
        except HttpError as e:
            logger.error(f"Errore ricerca messaggi Gmail: {e}")
            return []

        messages = result.get("messages", [])
        if not messages:
            return []

        output = []
        for ref in messages:
            msg = self._fetch_headers(ref["id"])
            if msg:
                output.append(msg)

        return output

    def _fetch_headers(self, message_id: str) -> EmailMessage | None:
        """Scarica solo gli header rilevanti del messaggio (chiamata leggera)."""
        try:
            data = (
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
        except HttpError as e:
            logger.error(f"Errore fetch messaggio {message_id}: {e}")
            return None

        headers = {
            h["name"]: h["value"]
            for h in data.get("payload", {}).get("headers", [])
        }

        from_header = headers.get("From", "")
        if not from_header:
            return None

        return EmailMessage(
            message_id=message_id,
            from_header=from_header,
            subject=headers.get("Subject", "(nessun oggetto)"),
            date=headers.get("Date", ""),
            thread_id=data.get("threadId", ""),
        )
