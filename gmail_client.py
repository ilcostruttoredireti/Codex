"""Gmail API client — authentication and message retrieval."""

import logging
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailClient:
    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._service = None

    # ── public interface ──────────────────────────────────────────────────────

    def authenticate(self) -> None:
        """
        Authenticate via OAuth2.
        On first run a browser window opens for the user to grant access.
        Subsequent runs reuse the saved token (auto-refreshed when expired).
        """
        creds: Optional[Credentials] = None

        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"credentials.json not found at '{self.credentials_file}'. "
                        "Download it from Google Cloud Console (OAuth2 Desktop App)."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, _SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(self.token_file, "w") as fh:
                fh.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authenticated successfully")

    def get_messages_since(
        self,
        since: Optional[datetime] = None,
        max_results: int = 50,
    ) -> list:
        """
        Return a list of parsed message dicts from the inbox.
        If *since* is provided only messages newer than that timestamp are returned.
        """
        query = "in:inbox -from:me -category:promotions -category:social"
        if since:
            query += f" after:{int(since.timestamp())}"

        try:
            response = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail list error: %s", exc)
            return []

        raw_list = response.get("messages", [])
        if not raw_list:
            return []

        parsed = []
        for item in raw_list:
            msg = self._fetch_message(item["id"])
            if msg:
                parsed.append(msg)

        logger.debug("Fetched %d messages from Gmail", len(parsed))
        return parsed

    # ── private helpers ───────────────────────────────────────────────────────

    def _fetch_message(self, message_id: str) -> Optional[dict]:
        """Retrieve and parse the metadata of a single message."""
        try:
            raw = (
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
        except HttpError as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
            return None

        headers = {h["name"]: h["value"] for h in raw["payload"]["headers"]}

        from_header = headers.get("From", "")
        if not from_header or "@" not in from_header:
            return None

        date_str = headers.get("Date", "")
        try:
            msg_date = parsedate_to_datetime(date_str)
        except Exception:
            msg_date = datetime.now(timezone.utc)

        return {
            "id": message_id,
            "raw_from": from_header,
            "subject": headers.get("Subject", "(no subject)"),
            "date": msg_date,
        }
