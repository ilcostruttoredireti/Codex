"""
Gmail client — wraps google-api-python-client for email polling.

Authentication uses OAuth2 with credentials stored in token.json.
Run `python auth.py` once to complete the OAuth flow.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Iterator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

_TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
_CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")


def _get_service():
    creds: Optional[Credentials] = None
    if os.path.exists(_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


class GmailClient:
    """Thin wrapper around the Gmail REST API."""

    def __init__(self):
        self._service = _get_service()

    # ------------------------------------------------------------------
    # Label helpers
    # ------------------------------------------------------------------

    def get_or_create_label(self, name: str) -> str:
        """Return the label ID for *name*, creating the label if needed."""
        labels = (
            self._service.users()
            .labels()
            .list(userId="me")
            .execute()
            .get("labels", [])
        )
        for label in labels:
            if label["name"] == name:
                return label["id"]
        result = (
            self._service.users()
            .labels()
            .create(userId="me", body={"name": name})
            .execute()
        )
        return result["id"]

    def add_label(self, message_id: str, label_id: str) -> None:
        self._service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    # ------------------------------------------------------------------
    # Message fetching
    # ------------------------------------------------------------------

    def fetch_inbox_messages(
        self,
        query: str = "in:inbox",
        max_results: int = 50,
        page_token: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """
        Return (messages, next_page_token).
        Each message dict has at minimum: id, threadId, from_header, subject.
        """
        params = dict(userId="me", q=query, maxResults=max_results)
        if page_token:
            params["pageToken"] = page_token

        resp = self._service.users().messages().list(**params).execute()
        raw_msgs = resp.get("messages", [])
        next_token = resp.get("nextPageToken")

        messages = []
        for m in raw_msgs:
            try:
                detail = (
                    self._service.users()
                    .messages()
                    .get(userId="me", id=m["id"], format="metadata",
                         metadataHeaders=["From", "Subject", "Date"])
                    .execute()
                )
                headers = {
                    h["name"].lower(): h["value"]
                    for h in detail.get("payload", {}).get("headers", [])
                }
                messages.append({
                    "id": m["id"],
                    "threadId": m["threadId"],
                    "from": headers.get("from", ""),
                    "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                    "labelIds": detail.get("labelIds", []),
                })
            except HttpError:
                continue
        return messages, next_token

    def iter_unprocessed_messages(
        self,
        processed_label: str = "HubSpot-Synced",
        batch_size: int = 50,
    ) -> Iterator[dict]:
        """
        Yield inbox messages that do NOT yet have *processed_label*.
        Handles pagination automatically.
        """
        label_id = self.get_or_create_label(processed_label)
        # Use Gmail search to exclude already-labelled messages
        query = f"in:inbox -label:{processed_label}"
        page_token = None
        while True:
            msgs, page_token = self.fetch_inbox_messages(
                query=query, max_results=batch_size, page_token=page_token
            )
            for msg in msgs:
                yield msg
            if not page_token:
                break

    def mark_as_processed(self, message_id: str, label_name: str = "HubSpot-Synced") -> None:
        label_id = self.get_or_create_label(label_name)
        self.add_label(message_id, label_id)
