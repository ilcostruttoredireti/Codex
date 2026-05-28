import json
import os
import re
from dataclasses import dataclass
from typing import Iterator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class SenderInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    display_name: Optional[str]
    domain: str
    message_id: str
    subject: str
    history_id: str


def _parse_from_header(from_header: str) -> tuple[Optional[str], str]:
    """Return (display_name, email) from a From header value."""
    match = re.match(r'"?([^"<]+?)"?\s*<([^>]+)>', from_header.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip().lower()
    # bare address
    return None, from_header.strip().lower()


def _split_name(display_name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not display_name:
        return None, None
    parts = display_name.strip().split()
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None

    def _authenticate(self):
        creds = None
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

    @property
    def service(self):
        if self._service is None:
            self._authenticate()
        return self._service

    def get_history_id(self) -> str:
        """Return the current Gmail history ID for later delta polling."""
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def new_messages_since(self, start_history_id: str) -> Iterator[dict]:
        """Yield raw message metadata for all new inbox messages since history_id."""
        page_token = None
        while True:
            kwargs = dict(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self.service.users().history().list(**kwargs).execute()
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    if "INBOX" in msg.get("labelIds", []):
                        yield msg
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def get_sender_info(self, message_id: str) -> Optional[SenderInfo]:
        """Fetch a message and return its SenderInfo, or None for system/bot senders."""
        msg = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata",
                 metadataHeaders=["From", "Subject"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "")
        if not from_header:
            return None

        display_name, email = _parse_from_header(from_header)
        domain = email.split("@")[-1] if "@" in email else ""
        first_name, last_name = _split_name(display_name)

        return SenderInfo(
            email=email,
            first_name=first_name,
            last_name=last_name,
            display_name=display_name,
            domain=domain,
            message_id=message_id,
            subject=subject,
            history_id=str(msg.get("historyId", "")),
        )
