"""Gmail API client with OAuth2 authentication and incremental sync via History API."""

import os
import logging
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.json"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._build_service()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _build_service(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"Gmail OAuth credentials file not found: {self.credentials_file}\n"
                        "Download it from the Google Cloud Console and set GMAIL_CREDENTIALS_FILE."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_current_history_id(self) -> str:
        """Return the current mailbox historyId."""
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def get_new_messages(self, start_history_id: str) -> list[dict]:
        """
        Return message stubs {id, threadId} for every message added to INBOX
        since *start_history_id*.  Falls back to get_recent_inbox_messages()
        when the history record has expired (404).
        """
        messages: list[dict] = []
        page_token = None

        try:
            while True:
                kwargs: dict = dict(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                if page_token:
                    kwargs["pageToken"] = page_token

                resp = self.service.users().history().list(**kwargs).execute()

                for history in resp.get("history", []):
                    for added in history.get("messagesAdded", []):
                        msg = added["message"]
                        if "INBOX" in msg.get("labelIds", []):
                            messages.append({"id": msg["id"], "threadId": msg["threadId"]})

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as exc:
            if exc.resp.status == 404:
                logger.warning("History record expired — falling back to recent message listing")
                return self.get_recent_inbox_messages(hours=24)
            raise

        return messages

    def get_recent_inbox_messages(self, hours: int = 24) -> list[dict]:
        """Return up to 100 inbox messages from the last *hours* hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        after_str = cutoff.strftime("%Y/%m/%d")

        resp = self.service.users().messages().list(
            userId="me",
            q=f"in:inbox after:{after_str}",
            maxResults=100,
        ).execute()

        return resp.get("messages", [])

    def get_message_sender(self, message_id: str) -> dict:
        """
        Fetch From / Subject / Date metadata for *message_id*.

        Returns:
            {id, from, subject, date}
        """
        msg = self.service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        return {
            "id": message_id,
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", "(nessun oggetto)"),
            "date": headers.get("Date", ""),
        }
