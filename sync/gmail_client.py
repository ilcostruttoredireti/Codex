"""Gmail API client — read-only inbox monitoring via historyId."""
import logging
import os
from typing import Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Addresses we never want to sync (automated senders)
_SKIP_PREFIXES = (
    "noreply@", "no-reply@", "donotreply@", "do-not-reply@",
    "notifications@", "mailer-daemon@", "postmaster@", "bounce@",
    "auto-reply@", "autoresponder@", "support@", "info@",
    "newsletter@", "marketing@", "unsubscribe@",
)


class GmailClient:
    def __init__(self, credentials_path: str, token_path: str):
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._service = None

    # ------------------------------------------------------------------
    # Service / auth
    # ------------------------------------------------------------------

    @property
    def service(self):
        if self._service is None:
            self._service = build("gmail", "v1", credentials=self._get_credentials(),
                                  cache_discovery=False)
        return self._service

    def _get_credentials(self) -> Credentials:
        creds: Optional[Credentials] = None
        if os.path.exists(self._token_path):
            creds = Credentials.from_authorized_user_file(self._token_path, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_path, "w") as f:
                f.write(creds.to_json())
        return creds

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def get_authenticated_email(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["emailAddress"].lower()

    def get_new_message_ids(self, since_history_id: str) -> Tuple[List[str], str]:
        """
        Return (new_message_ids, updated_history_id) for messages added to INBOX
        since *since_history_id*.  Falls back to listing recent messages when the
        historyId is too old (404 from the API).
        """
        try:
            history = (
                self.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=since_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning("historyId expired — falling back to recent INBOX messages")
                new_hid = self.get_current_history_id()
                return self._list_recent_inbox_ids(50), new_hid
            raise

        ids = []
        for record in history.get("history", []):
            for msg in record.get("messagesAdded", []):
                ids.append(msg["message"]["id"])

        new_hid = str(history.get("historyId", since_history_id))
        return list(dict.fromkeys(ids)), new_hid  # deduplicate, preserve order

    def get_messages_since_days(self, days: int) -> Tuple[List[str], str]:
        """Used for initial backfill only."""
        query = f"in:inbox newer_than:{days}d"
        result = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=500)
            .execute()
        )
        ids = [m["id"] for m in result.get("messages", [])]
        new_hid = self.get_current_history_id()
        return ids, new_hid

    def get_message_metadata(self, message_id: str) -> Optional[Dict]:
        """
        Fetch only the headers we need for contact extraction.
        Returns None on failure.
        """
        try:
            msg = (
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
        except HttpError as e:
            logger.error(f"Could not fetch message {message_id}: {e}")
            return None

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        return {
            "message_id": message_id,
            "from_header": headers.get("from", ""),
            "subject": headers.get("subject", "(no subject)"),
            "date": headers.get("date", ""),
        }

    @staticmethod
    def should_skip(email: str) -> bool:
        """Return True for automated / no-reply senders."""
        addr = email.lower()
        return any(addr.startswith(p) for p in _SKIP_PREFIXES)

    def _list_recent_inbox_ids(self, n: int) -> List[str]:
        result = (
            self.service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=n)
            .execute()
        )
        return [m["id"] for m in result.get("messages", [])]
