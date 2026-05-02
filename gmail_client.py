from __future__ import annotations

import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self.service = self._authenticate()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self):
        creds: Optional[Credentials] = None

        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, _SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_current_history_id(self) -> str:
        """Return the latest historyId from the mailbox profile."""
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def get_message_headers(self, message_id: str) -> Optional[dict]:
        """Fetch only the metadata headers for a single message."""
        try:
            return self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except Exception:
            return None

    def poll_new_inbox_messages(
        self, start_history_id: str
    ) -> tuple[list[str], str]:
        """
        Return (new_inbox_message_ids, latest_history_id) since start_history_id.

        Uses the Gmail History API so only a delta is fetched each poll cycle.
        """
        message_ids: list[str] = []
        latest_history_id = start_history_id
        page_token: Optional[str] = None

        while True:
            kwargs: dict = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
                "labelId": "INBOX",
            }
            if page_token:
                kwargs["pageToken"] = page_token

            response = self.service.users().history().list(**kwargs).execute()

            if "historyId" in response:
                latest_history_id = response["historyId"]

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    # Guard: only messages still carrying the INBOX label
                    if "INBOX" in msg.get("labelIds", []):
                        message_ids.append(msg["id"])

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return message_ids, latest_history_id
