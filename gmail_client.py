"""Gmail API client with incremental history-based polling."""

import os
import logging
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.json"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()
        self._profile_cache: dict | None = None

    def _authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def get_profile(self) -> dict:
        if self._profile_cache is None:
            self._profile_cache = self.service.users().getProfile(userId="me").execute()
        return self._profile_cache

    def get_new_messages(self, start_history_id: str) -> tuple[list[str], str]:
        """
        Return (list_of_new_message_ids, latest_history_id).
        Handles pagination and invalid historyId (404 → reset).
        """
        message_ids: list[str] = []
        new_history_id = start_history_id
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
                resp = self.service.users().history().list(**kwargs).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    # historyId expired — caller must reinitialize
                    raise HistoryExpiredError("historyId expired, reinitialize required") from e
                raise

            new_history_id = resp.get("historyId", new_history_id)
            seen: set[str] = set()
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_id = added["message"]["id"]
                    if msg_id not in seen:
                        seen.add(msg_id)
                        message_ids.append(msg_id)

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return message_ids, new_history_id

    def get_message_headers(self, message_id: str) -> dict:
        """Fetch only the metadata headers we need (minimal quota usage)."""
        msg = self.service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


class HistoryExpiredError(Exception):
    pass
