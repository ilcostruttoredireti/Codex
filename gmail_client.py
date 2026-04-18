"""Gmail API client – authentication and message fetching."""

import json
import os
from email.utils import parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
        user_id: str = "me",
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.user_id = user_id
        self.service = self._authenticate()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _authenticate(self):
        creds = None
        if Path(self.token_file).exists():
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ── Profile ───────────────────────────────────────────────────────────────

    def get_current_history_id(self) -> str:
        """Return the current historyId for the mailbox (used to seed state)."""
        profile = self.service.users().getProfile(userId=self.user_id).execute()
        return str(profile["historyId"])

    # ── History-based polling ─────────────────────────────────────────────────

    def get_new_messages(self, start_history_id: str) -> list[dict]:
        """
        Return sender info for messages added since *start_history_id*.
        Each item: {"message_id", "email", "name", "subject"}.
        """
        results = []
        page_token = None

        while True:
            try:
                kwargs = {
                    "userId": self.user_id,
                    "startHistoryId": start_history_id,
                    "historyTypes": ["messageAdded"],
                    "labelId": "INBOX",
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                history = self.service.users().history().list(**kwargs).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    # historyId expired; caller should re-seed
                    raise HistoryExpiredError("History ID expired") from e
                raise

            for record in history.get("history", []):
                for msg_added in record.get("messagesAdded", []):
                    msg = msg_added["message"]
                    # Skip sent/drafts – only INBOX messages
                    if "INBOX" not in msg.get("labelIds", []):
                        continue
                    sender = self._get_sender(msg["id"])
                    if sender:
                        results.append(sender)

            page_token = history.get("nextPageToken")
            if not page_token:
                break

        return results

    def get_latest_history_id(self, start_history_id: str) -> str:
        """
        Walk history pages and return the highest historyId seen.
        Falls back to *start_history_id* if there are no new records.
        """
        latest = start_history_id
        page_token = None

        while True:
            try:
                kwargs = {
                    "userId": self.user_id,
                    "startHistoryId": start_history_id,
                    "historyTypes": ["messageAdded"],
                }
                if page_token:
                    kwargs["pageToken"] = page_token
                history = self.service.users().history().list(**kwargs).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    raise HistoryExpiredError("History ID expired") from e
                raise

            for record in history.get("history", []):
                latest = max(latest, str(record["id"]), key=lambda x: int(x))

            page_token = history.get("nextPageToken")
            if not page_token:
                break

        return latest

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_sender(self, message_id: str) -> dict | None:
        """Fetch a single message and return sender info."""
        msg = (
            self.service.users()
            .messages()
            .get(userId=self.user_id, id=message_id, format="metadata",
                 metadataHeaders=["From", "Subject"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        subject = headers.get("Subject", "")

        if not raw_from:
            return None

        display_name, email_addr = parseaddr(raw_from)
        email_addr = email_addr.strip().lower()
        if not email_addr or "@" not in email_addr:
            return None

        return {
            "message_id": message_id,
            "email": email_addr,
            "name": display_name.strip(),
            "subject": subject,
        }


class HistoryExpiredError(Exception):
    """Raised when the stored historyId is too old and must be re-seeded."""
