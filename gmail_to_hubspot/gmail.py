"""Gmail API client."""

import logging
from pathlib import Path
from typing import Iterator, Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

logger = logging.getLogger(__name__)


class HistoryExpiredError(Exception):
    """Raised when the saved historyId is too old (Gmail keeps ~7 days)."""


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self.service = self._authenticate()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self):
        creds: Optional[Credentials] = None
        token_path = Path(self._token_file)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Profile helpers
    # ------------------------------------------------------------------

    def get_my_email(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["emailAddress"]

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    # ------------------------------------------------------------------
    # History-based polling
    # ------------------------------------------------------------------

    def get_new_messages_since(self, start_history_id: str) -> Iterator[dict]:
        """
        Yield message stubs {id, labelIds, …} for messages added to INBOX
        since *start_history_id*.

        Raises HistoryExpiredError if the stored ID has expired.
        """
        page_token: Optional[str] = None

        while True:
            kwargs: dict = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
                "labelId": "INBOX",
            }
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                resp = self.service.users().history().list(**kwargs).execute()
            except HttpError as exc:
                if exc.resp.status == 404:
                    raise HistoryExpiredError(
                        f"historyId {start_history_id} has expired"
                    ) from exc
                raise

            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []):
                        yield msg

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    # ------------------------------------------------------------------
    # Message metadata
    # ------------------------------------------------------------------

    def get_from_header(self, message_id: str) -> Optional[str]:
        """Return the From header of a message, or None on error."""
        try:
            msg = self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From"],
            ).execute()
            for header in msg.get("payload", {}).get("headers", []):
                if header["name"].lower() == "from":
                    return header["value"]
        except HttpError as exc:
            logger.warning("Cannot fetch message %s: %s", message_id, exc)
        return None
