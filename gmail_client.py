"""Gmail API client for monitoring incoming emails."""

import os
import json
import logging
from pathlib import Path
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path("state.json")


class GmailClient:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.json"):
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
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_history_id(self) -> str | None:
        """Return the stored historyId from state, or None if not set."""
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            return data.get("history_id")
        return None

    def save_history_id(self, history_id: str) -> None:
        data = {}
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
        data["history_id"] = history_id
        STATE_FILE.write_text(json.dumps(data))

    def get_latest_history_id(self) -> str:
        """Fetch the current historyId from the inbox (used on first run)."""
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def poll_new_messages(self) -> Generator[dict, None, None]:
        """
        Yield sender info dicts for each new incoming email since last poll.
        Each dict has keys: email, name, subject, message_id.
        """
        history_id = self.get_history_id()

        if history_id is None:
            # First run: record current position and exit without processing old mail.
            current = self.get_latest_history_id()
            self.save_history_id(current)
            logger.info("First run — saved historyId %s. Monitoring from now on.", current)
            return

        try:
            response = (
                self.service.users()
                .history()
                .list(userId="me", startHistoryId=history_id, historyTypes=["messageAdded"])
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId expired; reset to current position.
                current = self.get_latest_history_id()
                self.save_history_id(current)
                logger.warning("historyId expired. Reset to %s.", current)
                return
            raise

        new_history_id = response.get("historyId", history_id)
        self.save_history_id(new_history_id)

        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_stub = added["message"]
                # Skip sent mail (only process received messages).
                labels = msg_stub.get("labelIds", [])
                if "SENT" in labels or "DRAFT" in labels:
                    continue
                if "INBOX" not in labels and "UNREAD" not in labels:
                    continue

                sender = self._extract_sender(msg_stub["id"])
                if sender:
                    yield sender

    def _extract_sender(self, message_id: str) -> dict | None:
        try:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Subject"])
                .execute()
            )
        except HttpError:
            logger.exception("Failed to fetch message %s", message_id)
            return None

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "")

        email, name = _parse_from_header(from_header)
        if not email:
            return None

        return {
            "email": email.lower().strip(),
            "name": name.strip() if name else "",
            "subject": subject,
            "message_id": message_id,
        }


def _parse_from_header(from_header: str) -> tuple[str, str]:
    """
    Parse 'Display Name <email@example.com>' or 'email@example.com'.
    Returns (email, display_name).
    """
    from_header = from_header.strip()
    if "<" in from_header and ">" in from_header:
        name = from_header[: from_header.rfind("<")].strip().strip('"')
        email = from_header[from_header.rfind("<") + 1 : from_header.rfind(">")].strip()
        return email, name
    return from_header, ""
