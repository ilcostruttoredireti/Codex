"""Gmail API wrapper – reads inbox via OAuth2 and Gmail History API."""

import os
from email.utils import parseaddr
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"


class GmailClient:
    def __init__(self):
        self.service = self._authenticate()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    CREDENTIALS_FILE, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w") as fh:
                fh.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current_history_id(self) -> str:
        """Return the mailbox's latest historyId (used on first run)."""
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def get_new_messages(self, last_history_id: str | None, max_results: int = 50):
        """Return (messages, new_history_id) since last_history_id.

        Falls back to listing the most-recent *max_results* inbox messages
        when no history_id is provided or when the history has expired.
        """
        if last_history_id:
            try:
                history = (
                    self.service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=last_history_id,
                        historyTypes=["messageAdded"],
                        labelId="INBOX",
                    )
                    .execute()
                )
                messages = [
                    msg["message"]
                    for record in history.get("history", [])
                    for msg in record.get("messagesAdded", [])
                ]
                new_id = str(history.get("historyId", last_history_id))
                return messages, new_id
            except Exception:
                # historyId expired – fall through to list
                pass

        result = (
            self.service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        messages = result.get("messages", [])
        new_id = self.get_current_history_id()
        return messages, new_id

    def get_message_headers(self, message_id: str) -> dict:
        """Return From / Subject / Date headers for a single message."""
        msg = (
            self.service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        return {h["name"]: h["value"] for h in msg["payload"]["headers"]}
