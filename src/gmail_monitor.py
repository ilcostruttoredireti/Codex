"""Gmail monitor using the Gmail History API for incremental message polling."""

import base64
import logging
import os
from email.utils import parseaddr, parsedate_to_datetime
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailMonitor:
    def __init__(self, credentials_file: str, token_file: str, user_email: str = "me"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.user_email = user_email
        self.service = None
        self._authenticate()

    def _authenticate(self) -> None:
        creds = None
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
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        self.service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authentication successful")

    def get_current_history_id(self) -> str:
        """Fetch the current historyId from the latest message (used for first-run bootstrap)."""
        try:
            profile = self.service.users().getProfile(userId=self.user_email).execute()
            return str(profile.get("historyId", ""))
        except HttpError as e:
            logger.error("Failed to get Gmail profile: %s", e)
            raise

    def poll_new_messages(self, since_history_id: str) -> tuple[list[dict], str]:
        """
        Return (messages, new_history_id) for messages received after since_history_id.
        Each message dict contains extracted sender metadata.
        """
        messages = []
        new_history_id = since_history_id

        try:
            response = (
                self.service.users()
                .history()
                .list(
                    userId=self.user_email,
                    startHistoryId=since_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except HttpError as e:
            if e.resp.status == 404:
                # historyId too old – reset to current
                logger.warning("History ID expired, resetting to current")
                new_history_id = self.get_current_history_id()
                return [], new_history_id
            raise

        histories = response.get("history", [])
        new_history_id = str(response.get("historyId", since_history_id))

        message_ids_seen: set[str] = set()
        for history in histories:
            for added in history.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                if msg_id not in message_ids_seen:
                    message_ids_seen.add(msg_id)
                    msg_data = self._fetch_message_metadata(msg_id)
                    if msg_data:
                        messages.append(msg_data)

        logger.debug("Found %d new inbox messages", len(messages))
        return messages, new_history_id

    def _fetch_message_metadata(self, message_id: str) -> dict | None:
        """Fetch minimal metadata for a single message (headers only)."""
        try:
            msg = (
                self.service.users()
                .messages()
                .get(
                    userId=self.user_email,
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
        except HttpError as e:
            logger.warning("Could not fetch message %s: %s", message_id, e)
            return None

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        # Skip messages sent by the authenticated user (outgoing)
        from_header = headers.get("from", "")
        if not from_header:
            return None

        return {
            "id": message_id,
            "thread_id": msg.get("threadId", ""),
            "from": from_header,
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "snippet": msg.get("snippet", ""),
        }
