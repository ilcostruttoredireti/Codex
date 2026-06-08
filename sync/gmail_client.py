import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._service = self._authenticate(credentials_file, token_file)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @staticmethod
    def _authenticate(credentials_file: str, token_file: str):
        creds = None
        if os.path.exists(token_file):
            creds = Credentials.from_authorized_user_file(token_file, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_file, _SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_file, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_inbox_messages(self, max_results: int = 100) -> list[dict]:
        """Return a list of {id, threadId} dicts from the inbox."""
        try:
            result = (
                self._service.users()
                .messages()
                .list(userId="me", q="in:inbox -from:me", maxResults=max_results)
                .execute()
            )
            return result.get("messages", [])
        except HttpError as exc:
            log.error("Gmail list error: %s", exc)
            return []

    def get_from_header(self, msg_id: str) -> str | None:
        """Return the From: header value for the given message ID."""
        try:
            msg = (
                self._service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From"],
                )
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            return headers.get("From")
        except HttpError as exc:
            log.error("Gmail get message %s error: %s", msg_id, exc)
            return None

    def add_label(self, msg_id: str, label_id: str) -> None:
        """Add a label to the message (fire-and-forget)."""
        try:
            self._service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except HttpError as exc:
            log.warning("Could not label message %s: %s", msg_id, exc)

    def get_or_create_label(self, name: str) -> str | None:
        """Return the label ID, creating the label if it does not exist."""
        try:
            existing = self._service.users().labels().list(userId="me").execute()
            for lbl in existing.get("labels", []):
                if lbl["name"].lower() == name.lower():
                    return lbl["id"]
            created = (
                self._service.users()
                .labels()
                .create(userId="me", body={"name": name})
                .execute()
            )
            return created["id"]
        except HttpError as exc:
            log.warning("Could not get/create label '%s': %s", name, exc)
            return None
