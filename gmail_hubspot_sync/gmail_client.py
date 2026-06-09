import os
from typing import Iterator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self._service = self._authenticate(credentials_file, token_file)

    def _authenticate(self, credentials_file: str, token_file: str):
        creds: Optional[Credentials] = None
        if os.path.exists(token_file):
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(token_file, "w") as fh:
                fh.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def get_current_history_id(self) -> str:
        profile = self._service.users().getProfile(userId="me").execute()
        return profile.get("historyId", "")

    def list_new_messages(
        self,
        start_history_id: Optional[str] = None,
        max_results: int = 50,
    ) -> Iterator[dict]:
        """Yield message stubs {id, threadId} for INBOX messages.

        Uses the History API when start_history_id is available (efficient
        incremental sync). Falls back to listing the most recent messages on
        the first run.
        """
        if start_history_id:
            yield from self._messages_from_history(start_history_id)
        else:
            yield from self._messages_from_list(max_results)

    def _messages_from_history(self, start_history_id: str) -> Iterator[dict]:
        page_token: Optional[str] = None
        while True:
            kwargs: dict = dict(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self._service.users().history().list(**kwargs).execute()
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []):
                        yield msg
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def _messages_from_list(self, max_results: int) -> Iterator[dict]:
        resp = (
            self._service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        yield from resp.get("messages", [])

    def get_message_metadata(self, message_id: str) -> dict:
        """Fetch only From/Subject/Date headers — avoids downloading message bodies."""
        return (
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
