import os
import logging
from email.header import decode_header, make_header
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"Google credentials file not found: {self.credentials_file}\n"
                        "Download it from Google Cloud Console → APIs & Services → Credentials."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def get_new_message_ids(self, last_history_id: Optional[str]) -> list[str]:
        """
        Returns inbox message IDs added since last_history_id.
        Falls back to fetching recent unread messages on first run.
        """
        if last_history_id:
            try:
                return self._ids_from_history(last_history_id)
            except HttpError as exc:
                if exc.resp.status == 404:
                    logger.warning("History ID expired, falling back to recent messages.")
                else:
                    raise
        return self._ids_from_recent_inbox()

    def _ids_from_history(self, start_history_id: str) -> list[str]:
        ids: list[str] = []
        page_token = None

        while True:
            kwargs: dict = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
            }
            if page_token:
                kwargs["pageToken"] = page_token

            result = self.service.users().history().list(**kwargs).execute()

            for record in result.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []):
                        ids.append(msg["id"])

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return ids

    def _ids_from_recent_inbox(self, max_results: int = 100) -> list[str]:
        result = (
            self.service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in result.get("messages", [])]

    def get_message(self, message_id: str) -> dict:
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

    def extract_sender_info(self, message: dict) -> dict:
        headers = {
            h["name"]: h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "")
        name, email_addr = _parse_from_header(from_header)

        return {
            "message_id": message["id"],
            "name": name,
            "email": email_addr.lower(),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "raw_from": from_header,
        }


def _parse_from_header(from_header: str) -> tuple[str, str]:
    """Parse 'Display Name <addr>' or bare 'addr' format."""
    from_header = from_header.strip()

    if "<" in from_header:
        bracket_open = from_header.rfind("<")
        bracket_close = from_header.rfind(">")
        raw_name = from_header[:bracket_open].strip().strip('"').strip("'")
        email_addr = from_header[bracket_open + 1 : bracket_close].strip()

        try:
            raw_name = str(make_header(decode_header(raw_name)))
        except Exception:
            pass

        return raw_name, email_addr

    return "", from_header
