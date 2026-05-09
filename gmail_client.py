import os
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_SCOPES


class GmailClient:
    def __init__(self):
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if os.path.exists(GMAIL_TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(GMAIL_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def get_profile(self) -> dict:
        return self.service.users().getProfile(userId="me").execute()

    def list_inbox_messages(self, max_results: int = 50, page_token: str = None) -> tuple[list, str | None]:
        params = {"userId": "me", "labelIds": ["INBOX"], "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        result = self.service.users().messages().list(**params).execute()
        return result.get("messages", []), result.get("nextPageToken")

    def get_message_headers(self, message_id: str) -> dict:
        """Fetch only the metadata headers needed for contact extraction."""
        msg = self.service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Date", "Subject"],
        ).execute()
        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        return {
            "id": message_id,
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "subject": headers.get("Subject", ""),
            "internal_date": msg.get("internalDate", ""),
            "label_ids": msg.get("labelIds", []),
        }

    def list_history(
        self, start_history_id: str, label_id: str = "INBOX"
    ) -> tuple[list, str | None]:
        """Return new message events since *start_history_id*."""
        try:
            result = self.service.users().history().list(
                userId="me",
                startHistoryId=start_history_id,
                labelId=label_id,
                historyTypes=["messageAdded"],
            ).execute()
        except Exception as exc:
            # historyId may have expired (>7 days old); caller should fall back
            raise RuntimeError(f"History API error: {exc}") from exc
        return result.get("history", []), result.get("historyId")

    @staticmethod
    def internal_date_to_iso(ms_str: str) -> str:
        ts = int(ms_str) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
