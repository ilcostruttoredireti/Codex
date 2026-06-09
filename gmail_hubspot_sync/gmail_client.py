"""Gmail API client with OAuth2 authentication."""

import os
import pickle
from typing import Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(
        self,
        credentials_path: str = "credentials.json",
        token_path: str = "token.json",
    ):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.service = self._authenticate()

    def _authenticate(self):
        creds: Optional[Credentials] = None

        if os.path.exists(self.token_path):
            with open(self.token_path, "rb") as fh:
                creds = pickle.load(fh)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_path, "wb") as fh:
                pickle.dump(creds, fh)

        return build("gmail", "v1", credentials=creds)

    def list_inbox_messages(
        self,
        after_epoch: Optional[int] = None,
        max_results: int = 100,
    ) -> List[Dict]:
        """
        Return a list of {id, threadId} dicts from the inbox.
        If after_epoch is given, only messages received after that Unix timestamp.
        """
        query = "in:inbox -from:me"
        if after_epoch:
            query += f" after:{after_epoch}"

        response = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return response.get("messages", [])

    def get_message_headers(self, msg_id: str) -> Dict:
        """Fetch lightweight message metadata (From, Subject, Date headers)."""
        return (
            self.service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )

    @staticmethod
    def extract_header(message: Dict, name: str) -> Optional[str]:
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == name.lower():
                return h["value"]
        return None
