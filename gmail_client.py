from __future__ import annotations

from email.utils import parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self.service = self._authenticate()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self):
        creds: Credentials | None = None

        if Path(self._token_file).exists():
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_profile_history_id(self) -> str:
        """Return the current historyId from the authenticated mailbox."""
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def get_new_messages(self, start_history_id: str) -> list[dict]:
        """Return full metadata for inbox messages added since *start_history_id*.

        Uses the Gmail History API so only incremental changes are fetched on
        every poll cycle.  Returns an empty list when the history ID is still
        current (no new mail) or when it is too old (caller should reinitialise).
        """
        messages: list[dict] = []
        page_token: str | None = None

        while True:
            kwargs: dict = dict(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                response = self.service.users().history().list(**kwargs).execute()
            except HttpError as exc:
                # 404 means the historyId has expired; signal caller to reinitialise
                if exc.resp.status == 404:
                    raise HistoryIdExpiredError from exc
                raise

            for history in response.get("history", []):
                for added in history.get("messagesAdded", []):
                    raw = added["message"]
                    # The history entry only contains id + labelIds; fetch metadata
                    if "INBOX" in raw.get("labelIds", []):
                        full = self._fetch_metadata(raw["id"])
                        if full:
                            messages.append(full)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return messages

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_metadata(self, message_id: str) -> dict | None:
        try:
            return self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except HttpError:
            return None

    # ------------------------------------------------------------------
    # Static helpers (used by SyncEngine without a GmailClient instance)
    # ------------------------------------------------------------------

    @staticmethod
    def extract_sender(message: dict) -> tuple[str, str]:
        """Return ``(display_name, email_address)`` from a message's From header."""
        headers: list[dict] = message.get("payload", {}).get("headers", [])
        from_value = next(
            (h["value"] for h in headers if h["name"].lower() == "from"), ""
        )
        name, addr = parseaddr(from_value)
        return name.strip(), addr.strip().lower()

    @staticmethod
    def extract_subject(message: dict) -> str:
        headers: list[dict] = message.get("payload", {}).get("headers", [])
        return next(
            (h["value"] for h in headers if h["name"].lower() == "subject"), ""
        )


class HistoryIdExpiredError(Exception):
    """Raised when the stored historyId is too old to use with the History API."""
