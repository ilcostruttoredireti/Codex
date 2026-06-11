"""Gmail API client — list inbox messages and read headers."""

import os
from typing import Optional

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

    def _service_or_build(self):
        if self._service:
            return self._service

        creds: Optional[Credentials] = None

        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(self._token_file, "w") as fh:
                fh.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def list_inbox_message_ids(self, max_results: int = 100) -> list[str]:
        """Return message IDs for the inbox (newest first, excludes sent/drafts)."""
        svc = self._service_or_build()
        try:
            resp = (
                svc.users()
                .messages()
                .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
                .execute()
            )
            return [m["id"] for m in resp.get("messages", [])]
        except HttpError as exc:
            print(f"[Gmail] list error: {exc}")
            return []

    def get_message_info(self, message_id: str) -> dict:
        """Return From/Subject/Date headers plus threadId for a message."""
        svc = self._service_or_build()
        try:
            msg = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            return {
                "id": message_id,
                "thread_id": msg.get("threadId", ""),
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            }
        except HttpError as exc:
            print(f"[Gmail] get message {message_id} error: {exc}")
            return {}
