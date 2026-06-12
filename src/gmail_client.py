"""
Gmail API client — reads inbox threads and marks them as processed.
"""

import os
import json
import base64
from pathlib import Path
from typing import Iterator, Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

_PROCESSED_LABEL = "HubSpot-Synced"


class GmailClient:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.json"):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None
        self._processed_label_id: Optional[str] = None

    def _authenticate(self) -> Credentials:
        creds = None
        if Path(self._token_file).exists():
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self._credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            Path(self._token_file).write_text(creds.to_json())
        return creds

    def connect(self) -> None:
        creds = self._authenticate()
        self._service = build("gmail", "v1", credentials=creds)
        self._processed_label_id = self._get_or_create_label(_PROCESSED_LABEL)

    def _get_or_create_label(self, name: str) -> str:
        labels = self._service.users().labels().list(userId="me").execute()
        for label in labels.get("labels", []):
            if label["name"] == name:
                return label["id"]
        created = self._service.users().labels().create(
            userId="me",
            body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        ).execute()
        return created["id"]

    def unprocessed_messages(self, max_results: int = 50) -> Iterator[dict]:
        """Yield inbox messages that haven't been synced yet."""
        query = f"in:inbox -label:{_PROCESSED_LABEL}"
        page_token = None
        fetched = 0
        while fetched < max_results:
            resp = self._service.users().messages().list(
                userId="me", q=query, pageToken=page_token,
                maxResults=min(50, max_results - fetched),
            ).execute()
            for msg_ref in resp.get("messages", []):
                msg = self._service.users().messages().get(
                    userId="me", id=msg_ref["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                yield msg
                fetched += 1
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def get_message_snippet(self, message_id: str) -> str:
        msg = self._service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        payload = msg.get("payload", {})
        parts = payload.get("parts", [payload])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return msg.get("snippet", "")

    def get_header(self, message: dict, name: str) -> str:
        for h in message.get("payload", {}).get("headers", []):
            if h["name"].lower() == name.lower():
                return h["value"]
        return ""

    def mark_as_processed(self, message_id: str) -> None:
        if self._processed_label_id:
            self._service.users().messages().modify(
                userId="me", id=message_id,
                body={"addLabelIds": [self._processed_label_id]},
            ).execute()
