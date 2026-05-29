"""Gmail API client — fetches new inbound messages and extracts sender info."""

import email.utils
import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

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
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as fh:
                fh.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def get_history_id(self) -> str:
        """Return the current mailbox historyId."""
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def get_new_message_ids(self, since_history_id: str) -> tuple[list[str], str]:
        """
        Return (message_ids_added_since, new_history_id).
        Uses the Gmail history API for efficient incremental polling.
        """
        message_ids: list[str] = []
        page_token = None

        while True:
            kwargs = dict(
                userId="me",
                startHistoryId=since_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            if page_token:
                kwargs["pageToken"] = page_token

            result = self.service.users().history().list(**kwargs).execute()
            for record in result.get("history", []):
                for entry in record.get("messagesAdded", []):
                    message_ids.append(entry["message"]["id"])

            page_token = result.get("nextPageToken")
            if not page_token:
                return message_ids, result.get("historyId", since_history_id)

    def get_recent_message_ids(self, max_results: int = 50) -> list[str]:
        """Fallback: fetch the N most recent inbox messages (first run)."""
        result = (
            self.service.users()
            .messages()
            .list(userId="me", q="in:inbox", maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in result.get("messages", [])]

    def get_sender(self, message_id: str) -> dict | None:
        """
        Return a dict with sender details extracted from message headers,
        or None if the message has no usable From address.
        """
        msg = (
            self.service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From"],
            )
            .execute()
        )

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("from", "")
        display_name, addr = email.utils.parseaddr(from_header)
        if not addr or "@" not in addr:
            return None

        addr = addr.lower().strip()
        domain = addr.split("@")[1]

        parts = display_name.strip().split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        return {
            "email": addr,
            "display_name": display_name,
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "message_id": message_id,
        }
