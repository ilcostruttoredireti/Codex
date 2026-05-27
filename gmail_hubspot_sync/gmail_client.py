"""Gmail API client — fetches new messages using History API for efficiency."""

import json
import logging
import os
from pathlib import Path
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES, GMAIL_TOKEN_FILE

logger = logging.getLogger(__name__)


def _get_credentials() -> Credentials:
    creds = None
    token_path = Path(GMAIL_TOKEN_FILE)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return creds


class GmailClient:
    def __init__(self):
        creds = _get_credentials()
        self.service = build("gmail", "v1", credentials=creds)

    def get_profile(self) -> dict:
        return self.service.users().getProfile(userId="me").execute()

    def get_history_id(self) -> str:
        """Return the current historyId for the inbox."""
        profile = self.get_profile()
        return profile["historyId"]

    def iter_new_messages(self, start_history_id: str) -> Generator[dict, None, None]:
        """Yield raw message dicts for every new inbox message since start_history_id."""
        try:
            resp = (
                self.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except HttpError as exc:
            # historyId expired (older than ~30 days) — fall back to recent messages
            if exc.resp.status == 404:
                logger.warning("historyId expired, falling back to list query")
                yield from self._iter_recent_messages()
                return
            raise

        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                yield self._fetch_message(msg_id)

        next_page = resp.get("nextPageToken")
        while next_page:
            resp = (
                self.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                    pageToken=next_page,
                )
                .execute()
            )
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_id = added["message"]["id"]
                    yield self._fetch_message(msg_id)
            next_page = resp.get("nextPageToken")

    def _iter_recent_messages(self, max_results: int = 50) -> Generator[dict, None, None]:
        """Fallback: yield the most recent inbox messages."""
        resp = (
            self.service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        for msg in resp.get("messages", []):
            yield self._fetch_message(msg["id"])

    def _fetch_message(self, msg_id: str) -> dict:
        return (
            self.service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
