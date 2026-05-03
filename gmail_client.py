import os
import logging
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailClient:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.json"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None
        self._label_cache: dict[str, str] = {}

    def authenticate(self) -> "GmailClient":
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"File credenziali Gmail non trovato: {self.credentials_file}\n"
                        "Scarica credentials.json da Google Cloud Console "
                        "(APIs & Services → Credentials → OAuth 2.0 Client IDs)."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as fh:
                fh.write(creds.to_json())
            logger.info("Token Gmail salvato in %s", self.token_file)

        self.service = build("gmail", "v1", credentials=creds)
        logger.info("Autenticazione Gmail completata.")
        return self

    # ------------------------------------------------------------------
    # Message listing
    # ------------------------------------------------------------------

    def get_inbox_messages(self, after_timestamp: int | None = None, max_results: int = 100) -> list[dict]:
        """Return messages from INBOX, optionally only those after *after_timestamp* (Unix seconds)."""
        query = "in:inbox"
        if after_timestamp:
            query += f" after:{int(after_timestamp)}"

        try:
            result = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            return result.get("messages", [])
        except HttpError as exc:
            logger.error("Errore nel listare i messaggi: %s", exc)
            return []

    def get_message_metadata(self, message_id: str) -> dict | None:
        """Return metadata headers (From, Subject, Date) and labelIds for a message."""
        try:
            return (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
        except HttpError as exc:
            logger.error("Errore nel recuperare il messaggio %s: %s", message_id, exc)
            return None

    # ------------------------------------------------------------------
    # Label management
    # ------------------------------------------------------------------

    def get_or_create_label(self, label_name: str) -> str | None:
        """Return the label ID for *label_name*, creating it if it does not exist."""
        if label_name in self._label_cache:
            return self._label_cache[label_name]

        try:
            labels = self.service.users().labels().list(userId="me").execute()
            for lbl in labels.get("labels", []):
                if lbl["name"] == label_name:
                    self._label_cache[label_name] = lbl["id"]
                    return lbl["id"]

            created = (
                self.service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": label_name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            self._label_cache[label_name] = created["id"]
            logger.info("Etichetta Gmail creata: '%s' (id=%s)", label_name, created["id"])
            return created["id"]
        except HttpError as exc:
            logger.error("Errore nella gestione dell'etichetta '%s': %s", label_name, exc)
            return None

    def add_label(self, message_id: str, label_id: str) -> bool:
        """Add a label to a message. Returns True on success."""
        try:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
            return True
        except HttpError as exc:
            logger.error("Errore nell'aggiungere etichetta al messaggio %s: %s", message_id, exc)
            return False
