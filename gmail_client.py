"""Gmail API wrapper — reads incoming messages and parses sender info."""

import os
import re
import logging
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
logger = logging.getLogger(__name__)


class GmailClient:
    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
    ):
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
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current_history_id(self) -> str:
        """Return the mailbox's current history ID."""
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def get_new_messages(self, since_history_id: str | None = None) -> list[dict]:
        """Return new inbox messages since *since_history_id* (or recent unread)."""
        if since_history_id:
            return self._messages_from_history(since_history_id)
        return self._recent_unread_messages()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _recent_unread_messages(self, max_results: int = 100) -> list[dict]:
        """Fetch up to *max_results* recent unread inbox messages."""
        try:
            resp = (
                self.service.users()
                .messages()
                .list(userId="me", q="in:inbox is:unread", maxResults=max_results)
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail list error: %s", exc)
            return []

        raw = resp.get("messages", [])
        return [m for m in (self._fetch_message(r["id"]) for r in raw) if m]

    def _messages_from_history(self, start_history_id: str) -> list[dict]:
        """Use the History API to fetch only newly added inbox messages."""
        try:
            resp = (
                self.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except HttpError as exc:
            # historyId expired → fall back to recent unread
            logger.warning("History fetch failed (%s), falling back.", exc)
            return self._recent_unread_messages()

        messages = []
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = self._fetch_message(added["message"]["id"])
                if msg:
                    messages.append(msg)
        return messages

    def _fetch_message(self, message_id: str) -> dict | None:
        """Return a lightweight dict with sender metadata for *message_id*."""
        try:
            raw = (
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
        except HttpError as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
            return None

        headers = {h["name"]: h["value"] for h in raw["payload"]["headers"]}
        return {
            "id": message_id,
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
            "history_id": raw.get("historyId"),
        }

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def parse_sender(from_header: str) -> dict:
        """Parse ``'Name <email>'`` or bare ``email`` into a structured dict."""
        from_header = from_header.strip()
        match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', from_header)
        if match:
            name = match.group(1).strip().strip('"')
            email_addr = match.group(2).strip().lower()
        else:
            email_addr = from_header.lower()
            name = ""

        email_addr = re.sub(r"\s+", "", email_addr)  # remove any whitespace
        domain = email_addr.split("@")[1] if "@" in email_addr else ""

        parts = name.split(" ", 1) if name else []
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        return {
            "email": email_addr,
            "name": name,
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
        }
