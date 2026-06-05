"""Gmail API client — lists new inbox messages since a given history ID."""

import os
import json
import base64
from pathlib import Path
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _authenticate(credentials_file: str, token_file: str) -> Credentials:
    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, _SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())

    return creds


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        creds = _authenticate(credentials_file, token_file)
        self._service = build("gmail", "v1", credentials=creds)

    def get_history_id(self) -> str:
        """Return the current mailbox historyId (used as a starting cursor)."""
        profile = self._service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def messages_since(self, start_history_id: str) -> Iterator[dict]:
        """
        Yield minimal message dicts ({id, threadId}) for every new INBOX
        message added after start_history_id.
        """
        page_token = None
        while True:
            kwargs = dict(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                resp = self._service.users().history().list(**kwargs).execute()
            except HttpError as exc:
                # 404 means the historyId is too old — caller should reset
                if exc.resp.status == 404:
                    raise HistoryExpiredError(start_history_id) from exc
                raise

            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    yield added["message"]

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def get_from_header(self, message_id: str) -> str:
        """Return the raw From header value for a message."""
        msg = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From"])
            .execute()
        )
        for header in msg.get("payload", {}).get("headers", []):
            if header["name"].lower() == "from":
                return header["value"]
        return ""


class HistoryExpiredError(Exception):
    """Raised when the stored historyId is too old for the Gmail History API."""
