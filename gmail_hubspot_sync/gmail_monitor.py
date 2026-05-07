"""Gmail API wrapper for monitoring incoming emails."""

import os
import json
import logging
from pathlib import Path
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"


class GmailMonitor:
    def __init__(self, credentials_path: str = CREDENTIALS_FILE, token_path: str = TOKEN_FILE):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None

        if Path(self.token_path).exists():
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not Path(self.credentials_path).exists():
                    raise FileNotFoundError(
                        f"Credentials file '{self.credentials_path}' not found. "
                        "Download it from Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(self.token_path, "w") as token_file:
                token_file.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_history_id(self) -> str | None:
        """Return the current mailbox historyId."""
        try:
            profile = self.service.users().getProfile(userId="me").execute()
            return profile.get("historyId")
        except HttpError as e:
            logger.error("Failed to get Gmail profile: %s", e)
            return None

    def get_new_messages(self, start_history_id: str) -> Generator[dict, None, None]:
        """Yield new INBOX messages since start_history_id."""
        page_token = None
        while True:
            try:
                kwargs = {
                    "userId": "me",
                    "startHistoryId": start_history_id,
                    "historyTypes": ["messageAdded"],
                    "labelId": "INBOX",
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                response = self.service.users().history().list(**kwargs).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    # historyId expired – caller should reset
                    logger.warning("History ID expired; resetting state.")
                    raise HistoryExpiredError() from e
                logger.error("Gmail history.list error: %s", e)
                break

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []) and "SENT" not in msg.get("labelIds", []):
                        yield msg

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    def get_message_detail(self, message_id: str) -> dict | None:
        """Fetch full message metadata."""
        try:
            return (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError as e:
            logger.error("Failed to fetch message %s: %s", message_id, e)
            return None

    def get_recent_inbox_messages(self, max_results: int = 100) -> list[dict]:
        """Return recent INBOX messages (used for initial seeding)."""
        try:
            response = (
                self.service.users()
                .messages()
                .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
                .execute()
            )
            return response.get("messages", [])
        except HttpError as e:
            logger.error("Failed to list messages: %s", e)
            return []


class HistoryExpiredError(Exception):
    """Raised when the Gmail history ID is no longer valid."""
