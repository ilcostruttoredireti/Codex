import logging
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds: Optional[Credentials] = None

        if Path(self.token_file).exists():
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("Gmail token refreshed")
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
                logger.info("Gmail authentication completed")
            Path(self.token_file).write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_profile(self) -> dict[str, Any]:
        return self.service.users().getProfile(userId="me").execute()

    def list_messages(
        self, query: str = "in:inbox is:unread", max_results: int = 100
    ) -> list[dict]:
        try:
            result = self.service.users().messages().list(
                userId="me", q=query, maxResults=max_results
            ).execute()
            return result.get("messages", [])
        except HttpError as e:
            logger.error(f"Error listing messages: {e}")
            return []

    def get_message(self, message_id: str) -> Optional[dict[str, Any]]:
        try:
            return self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except HttpError as e:
            logger.error(f"Error fetching message {message_id}: {e}")
            return None

    def get_history(
        self, start_history_id: str, label_id: str = "INBOX"
    ) -> dict[str, Any]:
        """Returns new messages added since start_history_id.

        Returns {"expired": True} when the history ID is too old (Gmail
        discards history after ~7 days).
        """
        try:
            return self.service.users().history().list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId=label_id,
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning(
                    f"History ID {start_history_id} expired, will resync"
                )
                return {"history": [], "expired": True}
            raise
