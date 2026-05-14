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
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._build_service()

    def _build_service(self):
        creds = self._get_credentials()
        return build("gmail", "v1", credentials=creds)

    def _get_credentials(self) -> Credentials:
        creds: Optional[Credentials] = None

        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as fh:
                fh.write(creds.to_json())

        return creds

    def get_profile(self) -> dict:
        return self.service.users().getProfile(userId="me").execute()

    def get_history(self, start_history_id: str, page_token: Optional[str] = None) -> dict:
        kwargs = {
            "userId": "me",
            "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded"],
            "labelId": "INBOX",
        }
        if page_token:
            kwargs["pageToken"] = page_token
        return self.service.users().history().list(**kwargs).execute()

    def get_message_metadata(self, message_id: str) -> dict:
        """Fetch only From, Subject, Date headers — lighter than a full fetch."""
        return (
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

    def collect_new_message_ids(self, history_id: str) -> tuple[str, list[str]]:
        """
        Walk the history feed and return (new_history_id, [message_ids]).
        Raises GmailHistoryExpired if the historyId is no longer valid.
        """
        new_history_id = history_id
        message_ids: list[str] = []
        page_token: Optional[str] = None

        while True:
            try:
                data = self.get_history(history_id, page_token)
            except HttpError as exc:
                if exc.resp.status == 404:
                    raise GmailHistoryExpired("historyId scaduto") from exc
                raise

            new_history_id = data.get("historyId", new_history_id)

            for record in data.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []):
                        msg_id = msg["id"]
                        if msg_id not in message_ids:
                            message_ids.append(msg_id)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return new_history_id, message_ids


class GmailHistoryExpired(Exception):
    pass
