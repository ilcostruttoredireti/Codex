import os
from typing import Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class HistoryExpiredError(Exception):
    """Raised when the stored historyId is no longer valid."""


class GmailClient:
    def __init__(self, credentials_path: str, token_path: str):
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._service = self._authenticate()

    def _authenticate(self):
        creds: Optional[Credentials] = None

        if os.path.exists(self._token_path):
            creds = Credentials.from_authorized_user_file(self._token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_path, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_profile(self) -> Dict:
        """Return Gmail profile (contains current historyId)."""
        return self._service.users().getProfile(userId="me").execute()

    def get_new_message_ids(
        self, start_history_id: str
    ) -> Tuple[List[str], str]:
        """
        Return (list_of_new_inbox_message_ids, new_history_id) since
        start_history_id.  Raises HistoryExpiredError if the ID is stale.
        """
        message_ids: List[str] = []
        page_token: Optional[str] = None
        new_history_id = start_history_id

        while True:
            kwargs: Dict = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
                "labelId": "INBOX",
            }
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                result = (
                    self._service.users().history().list(**kwargs).execute()
                )
            except HttpError as exc:
                if exc.status_code == 404:
                    raise HistoryExpiredError(
                        f"historyId {start_history_id} is no longer valid"
                    ) from exc
                raise

            new_history_id = result.get("historyId", new_history_id)

            for record in result.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    # Confirm message is still in INBOX (not immediately moved)
                    if "INBOX" in msg.get("labelIds", []):
                        message_ids.append(msg["id"])

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return message_ids, new_history_id

    def get_message_headers(self, message_id: str) -> Dict[str, str]:
        """
        Return a dict of selected headers for a message:
        {'from', 'subject', 'date'}.
        """
        msg = (
            self._service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        raw = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        return {
            "message_id": message_id,
            "from": raw.get("from", ""),
            "subject": raw.get("subject", ""),
            "date": raw.get("date", ""),
        }
