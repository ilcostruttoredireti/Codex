import os
import pickle
import logging
from typing import List, Optional

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.pickle"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None

        if os.path.exists(self.token_file):
            with open(self.token_file, "rb") as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(self.token_file, "wb") as f:
                pickle.dump(creds, f)

        return build("gmail", "v1", credentials=creds)

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def get_new_messages_since(self, start_history_id: str) -> List[dict]:
        """Return list of {id, threadId} for messages added to INBOX since the given historyId."""
        messages = []
        page_token = None

        while True:
            try:
                kwargs = dict(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                if page_token:
                    kwargs["pageToken"] = page_token

                response = self.service.users().history().list(**kwargs).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    # historyId too old, caller must reset
                    logger.warning("historyId expired (404). Will reset on next cycle.")
                    return []
                raise

            for record in response.get("history", []):
                for msg in record.get("messagesAdded", []):
                    messages.append(msg["message"])

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return messages

    def get_message_headers(self, message_id: str) -> Optional[dict]:
        """Return {'from': ..., 'subject': ..., 'date': ...} or None on error."""
        try:
            msg = self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()

            headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}
            return {
                "id": message_id,
                "from": headers.get("from", ""),
                "subject": headers.get("subject", "(no subject)"),
                "date": headers.get("date", ""),
                "history_id": msg.get("historyId", ""),
            }
        except HttpError as e:
            logger.error("Failed to fetch message %s: %s", message_id, e)
            return None
