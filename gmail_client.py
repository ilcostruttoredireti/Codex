import logging
import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def connect(self):
        creds = self._load_or_refresh_credentials()
        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Connected to Gmail API")

    def _load_or_refresh_credentials(self) -> Credentials:
        creds: Optional[Credentials] = None

        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, _SCOPES
                )
                creds = flow.run_local_server(port=0)

            os.makedirs(os.path.dirname(self._token_file) or ".", exist_ok=True)
            with open(self._token_file, "w") as fh:
                fh.write(creds.to_json())

        return creds

    # ------------------------------------------------------------------
    # Message fetching
    # ------------------------------------------------------------------

    def fetch_new_messages(
        self, after_history_id: Optional[str]
    ) -> tuple[list[dict], str]:
        """
        Return (messages, new_history_id).

        On the first run (after_history_id is None) return the most recent
        unread inbox messages and record the current history ID so that
        subsequent calls only process truly new arrivals.
        """
        profile = self._service.users().getProfile(userId="me").execute()
        current_history_id: str = profile["historyId"]

        if after_history_id is None:
            messages = self._list_unread_inbox(max_results=50)
            return messages, current_history_id

        if after_history_id == current_history_id:
            return [], current_history_id

        messages = self._list_via_history(after_history_id)
        return messages, current_history_id

    def _list_unread_inbox(self, max_results: int = 50) -> list[dict]:
        result = (
            self._service.users()
            .messages()
            .list(userId="me", q="is:unread in:inbox", maxResults=max_results)
            .execute()
        )
        return [
            msg
            for ref in result.get("messages", [])
            if (msg := self._get_message(ref["id"])) is not None
        ]

    def _list_via_history(self, start_history_id: str) -> list[dict]:
        try:
            history_result = (
                self._service.users()
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
            # History expired (410) — fall back to unread scan
            logger.warning(f"History lookup failed ({exc.status_code}), falling back to unread scan")
            return self._list_unread_inbox()

        seen: set[str] = set()
        messages: list[dict] = []
        for item in history_result.get("history", []):
            for added in item.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                if msg_id not in seen:
                    seen.add(msg_id)
                    msg = self._get_message(msg_id)
                    if msg:
                        messages.append(msg)
        return messages

    def _get_message(self, msg_id: str) -> Optional[dict]:
        try:
            return (
                self._service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
        except HttpError as exc:
            logger.error(f"Could not fetch message {msg_id}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_header(self, message: dict, name: str) -> Optional[str]:
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == name.lower():
                return h["value"]
        return None
