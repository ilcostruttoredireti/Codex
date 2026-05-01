"""Gmail API client for monitoring the inbox and retrieving message metadata."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

logger = logging.getLogger(__name__)


class GmailClient:
    def __init__(self, credentials_path: str = "credentials.json", token_path: str = "token.json"):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.service = self._authenticate()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self):
        creds: Credentials | None = None

        if Path(self.token_path).exists():
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(self.token_path, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_profile(self) -> dict:
        return self.service.users().getProfile(userId="me").execute()

    def get_new_messages(self, last_history_id: str | None) -> tuple[list[dict], str]:
        """
        Return (list_of_message_stubs, new_history_id).

        On the first call (no last_history_id) we fetch the most recent inbox
        messages and return them so they are processed, then record the current
        historyId for incremental polling on subsequent calls.
        """
        profile = self.get_profile()
        current_history_id: str = profile["historyId"]

        if last_history_id:
            messages = self._incremental_messages(last_history_id)
        else:
            messages = self._initial_messages()

        return messages, current_history_id

    def get_message_headers(self, message_id: str) -> dict[str, str]:
        """
        Return a dict of {header_name: value} for the relevant headers of a
        single message.  Uses the lightweight 'metadata' format to avoid
        fetching the full body.
        """
        try:
            msg = self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except HttpError as exc:
            logger.error("Could not fetch message %s: %s", message_id, exc)
            return {}

        headers = msg.get("payload", {}).get("headers", [])
        return {h["name"]: h["value"] for h in headers}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _initial_messages(self) -> list[dict]:
        """Fetch a small initial batch of inbox messages (first-run bootstrap)."""
        result = (
            self.service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=25)
            .execute()
        )
        return result.get("messages", [])

    def _incremental_messages(self, start_history_id: str) -> list[dict]:
        """Use the History API to get only messages added since last run."""
        messages: list[dict] = []
        page_token = None

        while True:
            kwargs: dict = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
                "labelId": "INBOX",
            }
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                response = self.service.users().history().list(**kwargs).execute()
            except HttpError as exc:
                # historyId too old → fall back to listing recent messages
                if exc.resp.status == 404:
                    logger.warning("historyId expired, falling back to full list")
                    return self._initial_messages()
                raise

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    messages.append(added["message"])

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return messages
