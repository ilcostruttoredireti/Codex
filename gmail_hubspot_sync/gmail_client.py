"""Gmail API wrapper using OAuth2 device/installed-app flow."""

import logging
import os
from typing import Iterator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from models import SenderInfo  # noqa: re-export for backward compat

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None

    def authenticate(self) -> None:
        creds: Optional[Credentials] = None
        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "w") as f:
                f.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authentication successful.")

    @property
    def service(self):
        if self._service is None:
            raise RuntimeError("Call authenticate() first.")
        return self._service

    def get_initial_history_id(self) -> str:
        """Return the historyId of the user's mailbox right now (start point)."""
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def poll_new_messages(self, since_history_id: str) -> Iterator[SenderInfo]:
        """Yield SenderInfo for each new inbound message since `since_history_id`."""
        page_token = None
        new_history_id = since_history_id

        while True:
            try:
                params = {
                    "userId": "me",
                    "startHistoryId": since_history_id,
                    "historyTypes": ["messageAdded"],
                    "labelId": "INBOX",
                }
                if page_token:
                    params["pageToken"] = page_token

                response = self.service.users().history().list(**params).execute()
            except HttpError as exc:
                if exc.resp.status == 404:
                    # historyId too old; caller must reset
                    raise HistoryExpiredError() from exc
                raise

            new_history_id = response.get("historyId", new_history_id)

            for record in response.get("history", []):
                for msg_added in record.get("messagesAdded", []):
                    msg = msg_added.get("message", {})
                    # Skip sent mail and drafts
                    labels = msg.get("labelIds", [])
                    if "SENT" in labels or "DRAFT" in labels:
                        continue
                    sender = self._fetch_sender(msg["id"])
                    if sender:
                        yield sender

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        # Update caller's reference by returning via attribute
        self._last_history_id = new_history_id

    @property
    def last_history_id(self) -> str:
        return getattr(self, "_last_history_id", "")

    def _fetch_sender(self, message_id: str) -> Optional[SenderInfo]:
        try:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
        except HttpError as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
            return None

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        subject = headers.get("Subject", "")

        if not raw_from:
            return None

        info = SenderInfo(raw_from, subject, message_id)
        if not info.email or "@" not in info.email:
            return None
        return info


class HistoryExpiredError(Exception):
    """Raised when the Gmail historyId is too old and must be reset."""
