"""Gmail API client with OAuth2 authentication and incremental polling."""
import base64
import email as email_lib
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import Config

logger = logging.getLogger(__name__)


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    full_name: str
    domain: str
    message_id: str
    subject: str


class GmailClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.service = None

    def authenticate(self) -> None:
        creds: Optional[Credentials] = None
        token_file = self.config.gmail_token_file

        if os.path.exists(token_file):
            creds = Credentials.from_authorized_user_file(token_file, self.config.gmail_scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.config.gmail_credentials_file,
                    self.config.gmail_scopes,
                )
                creds = flow.run_local_server(port=0)
            with open(token_file, "w") as f:
                f.write(creds.to_json())

        self.service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authentication successful")

    def get_current_history_id(self) -> str:
        """Return the historyId of the user's mailbox right now."""
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def get_new_messages(self, since_history_id: str) -> list[dict]:
        """
        Return raw message stubs for all messages received since *since_history_id*.
        Uses the History API so we only fetch incremental changes.
        """
        messages: list[dict] = []
        page_token = None

        try:
            while True:
                kwargs: dict = {
                    "userId": "me",
                    "startHistoryId": since_history_id,
                    "historyTypes": ["messageAdded"],
                    "labelId": "INBOX",
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                resp = self.service.users().history().list(**kwargs).execute()
                for record in resp.get("history", []):
                    for added in record.get("messagesAdded", []):
                        msg = added.get("message", {})
                        # Only process messages that are still in INBOX and not sent by us
                        labels = msg.get("labelIds", [])
                        if "INBOX" in labels and "SENT" not in labels:
                            messages.append(msg)

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId too old — start fresh
                logger.warning("History ID expired, resetting state")
                return []
            raise

        return messages

    def get_message_details(self, message_id: str) -> Optional[SenderInfo]:
        """Fetch full message and extract sender info."""
        try:
            msg = self.service.users().messages().get(
                userId="me", id=message_id, format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
        except HttpError as exc:
            logger.error("Failed to fetch message %s: %s", message_id, exc)
            return None

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "(no subject)")

        return self._parse_from_header(from_header, message_id, subject)

    @staticmethod
    def _parse_from_header(from_header: str, message_id: str, subject: str) -> Optional[SenderInfo]:
        """
        Parse the RFC 2822 From header.
        Accepted formats:
          "First Last <user@domain.com>"
          "user@domain.com"
        """
        from_header = from_header.strip()
        if not from_header:
            return None

        parsed = email_lib.utils.parseaddr(from_header)
        full_name, addr = parsed

        if not addr or "@" not in addr:
            return None

        addr = addr.lower().strip()
        domain = addr.split("@", 1)[1]

        full_name = full_name.strip()
        parts = full_name.split(None, 1) if full_name else []
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        return SenderInfo(
            email=addr,
            first_name=first_name,
            last_name=last_name,
            full_name=full_name,
            domain=domain,
            message_id=message_id,
            subject=subject,
        )
