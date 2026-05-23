from __future__ import annotations
import os
import logging
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
logger = logging.getLogger(__name__)


@dataclass
class SenderInfo:
    email: str
    name: str
    first_name: str
    last_name: str
    domain: str


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
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
        return build("gmail", "v1", credentials=creds)

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def get_new_inbox_message_ids(self, since_history_id: str) -> tuple[list[str], str]:
        """Returns (new_message_ids, latest_history_id) since the given historyId."""
        message_ids: list[str] = []
        latest_id = since_history_id
        page_token: Optional[str] = None

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

                result = self.service.users().history().list(**kwargs).execute()
                latest_id = str(result.get("historyId", latest_id))

                for record in result.get("history", []):
                    for added in record.get("messagesAdded", []):
                        msg = added.get("message", {})
                        labels = msg.get("labelIds", [])
                        if "INBOX" in labels and "SPAM" not in labels and "TRASH" not in labels:
                            message_ids.append(msg["id"])

                page_token = result.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId too old — fall back to listing recent inbox messages
                logger.warning("historyId expired; fetching recent inbox messages instead")
                ids = self._list_recent_inbox_ids()
                latest_id = self.get_current_history_id()
                return ids, latest_id
            raise

        return message_ids, latest_id

    def _list_recent_inbox_ids(self, max_results: int = 100) -> list[str]:
        result = (
            self.service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in result.get("messages", [])]

    def get_sender_info(self, message_id: str) -> Optional[SenderInfo]:
        try:
            msg = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From"],
                )
                .execute()
            )
        except HttpError as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
            return None

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "").strip()
        return self._parse_sender(from_header) if from_header else None

    @staticmethod
    def _parse_sender(from_header: str) -> Optional[SenderInfo]:
        name, email_addr = parseaddr(from_header)
        email_addr = email_addr.lower().strip()
        if not email_addr or "@" not in email_addr:
            return None

        domain = email_addr.split("@")[1]
        name = name.strip().strip('"')

        parts = name.split()
        first_name = parts[0] if parts else ""
        last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

        return SenderInfo(
            email=email_addr,
            name=name,
            first_name=first_name,
            last_name=last_name,
            domain=domain,
        )
