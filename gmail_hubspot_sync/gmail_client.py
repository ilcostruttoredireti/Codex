import os
import logging
from typing import List, Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

logger = logging.getLogger(__name__)


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def get_inbox_messages(
        self,
        after_unix_ts: Optional[int] = None,
        max_results: int = 50,
    ) -> List[dict]:
        """Return inbox messages, optionally filtered to those after a Unix timestamp."""
        query = "in:inbox"
        if after_unix_ts:
            query += f" after:{after_unix_ts}"

        try:
            result = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            message_stubs = result.get("messages", [])
        except HttpError as e:
            logger.error("Gmail list error: %s", e)
            return []

        messages = []
        for stub in message_stubs:
            try:
                msg = (
                    self.service.users()
                    .messages()
                    .get(
                        userId="me",
                        messageId=stub["id"],
                        format="metadata",
                        metadataHeaders=["From", "Subject", "Date"],
                    )
                    .execute()
                )
                messages.append(msg)
            except HttpError as e:
                logger.warning("Could not fetch message %s: %s", stub["id"], e)
        return messages

    @staticmethod
    def get_header(message: dict, name: str) -> Optional[str]:
        for h in message.get("payload", {}).get("headers", []):
            if h["name"].lower() == name.lower():
                return h["value"]
        return None
