"""Gmail API wrapper — read-only inbox access with OAuth2."""

import logging
import os
import pickle
from typing import Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailReader:
    def __init__(
        self,
        credentials_path: str = "credentials.json",
        token_path: str = "token.pickle",
    ):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.service = self._authenticate()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_messages(
        self,
        since_epoch: Optional[int] = None,
        max_results: int = 50,
    ) -> List[Dict]:
        """Fetch inbox messages (metadata only).

        Args:
            since_epoch: If given, only return messages received after this
                         Unix timestamp (seconds).
            max_results:  Maximum number of messages to fetch per call.

        Returns:
            List of Gmail message resource dicts with From/Subject/Date headers.
        """
        query = "in:inbox -from:me"
        if since_epoch:
            query += f" after:{since_epoch}"

        try:
            list_resp = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
        except Exception as exc:
            logger.error("Gmail list error: %s", exc)
            return []

        stubs = list_resp.get("messages", [])
        if not stubs:
            return []

        messages: List[Dict] = []
        for stub in stubs:
            try:
                msg = (
                    self.service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=stub["id"],
                        format="metadata",
                        metadataHeaders=["From", "Subject", "Date"],
                    )
                    .execute()
                )
                messages.append(msg)
            except Exception as exc:
                logger.warning("Could not fetch message %s: %s", stub["id"], exc)

        return messages

    @staticmethod
    def get_header(message: Dict, name: str) -> Optional[str]:
        """Return the value of a named header from a message dict."""
        for header in message.get("payload", {}).get("headers", []):
            if header.get("name", "").lower() == name.lower():
                return header.get("value")
        return None
