import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


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
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def get_new_message_ids(self, since_history_id: str) -> list[str]:
        """Return message IDs added to INBOX since *since_history_id*."""
        ids: list[str] = []
        page_token = None

        while True:
            kwargs: dict = dict(
                userId="me",
                startHistoryId=since_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                resp = self.service.users().history().list(**kwargs).execute()
            except HttpError as exc:
                if exc.resp.status == 404:
                    # History ID expired – caller must fall back to a full list
                    raise HistoryExpiredError(since_history_id) from exc
                raise

            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []):
                        ids.append(msg["id"])

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return ids

    def list_inbox_message_ids(self, max_results: int = 100) -> list[str]:
        """List recent INBOX message IDs (used on first run / history expiry)."""
        ids: list[str] = []
        page_token = None

        while len(ids) < max_results:
            kwargs: dict = dict(
                userId="me",
                labelIds=["INBOX"],
                maxResults=min(500, max_results - len(ids)),
            )
            if page_token:
                kwargs["pageToken"] = page_token

            resp = self.service.users().messages().list(**kwargs).execute()
            ids.extend(m["id"] for m in resp.get("messages", []))

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return ids

    def get_message_metadata(self, message_id: str) -> dict:
        return self.service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_sender(message: dict) -> tuple[str, str]:
        """Return (display_name, email_address) from a message's headers."""
        headers = message.get("payload", {}).get("headers", [])
        from_value = next(
            (h["value"] for h in headers if h["name"].lower() == "from"), ""
        )
        return _parse_from_header(from_value)

    @staticmethod
    def get_subject(message: dict) -> str:
        headers = message.get("payload", {}).get("headers", [])
        return next(
            (h["value"] for h in headers if h["name"].lower() == "subject"), ""
        )


class HistoryExpiredError(Exception):
    """Raised when Gmail history ID is too old and has been purged."""


def _parse_from_header(value: str) -> tuple[str, str]:
    """Parse 'Display Name <addr@example.com>' or bare 'addr@example.com'."""
    value = value.strip()
    if "<" in value and ">" in value:
        langle = value.index("<")
        rangle = value.index(">")
        name = value[:langle].strip().strip('"').strip("'")
        addr = value[langle + 1 : rangle].strip()
    else:
        name = ""
        addr = value
    return name, addr.lower()
