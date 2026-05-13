"""Gmail API client: OAuth authentication and inbox polling via history API."""

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None

    def authenticate(self) -> None:
        creds = None
        if Path(self._token_file).exists():
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "w") as f:
                f.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)

    def get_current_history_id(self) -> str:
        """Return the current historyId from the user's Gmail profile."""
        profile = self._service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def fetch_new_messages(self, start_history_id: str) -> list[dict]:
        """
        Return a list of raw message dicts for messages added to INBOX
        since start_history_id.
        """
        messages = []
        page_token = None

        while True:
            kwargs = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
                "labelId": "INBOX",
            }
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                response = self._service.users().history().list(**kwargs).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    # historyId expired; caller must reset
                    raise HistoryExpiredError(str(e)) from e
                raise

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_id = added["message"]["id"]
                    full = self._get_message(msg_id)
                    if full:
                        messages.append(full)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return messages

    def _get_message(self, msg_id: str) -> dict | None:
        try:
            return (
                self._service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError:
            return None


class HistoryExpiredError(Exception):
    """Raised when Gmail historyId is too old and must be reset."""
