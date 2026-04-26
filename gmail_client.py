import email.utils
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class InvalidHistoryIdError(Exception):
    pass


class GmailClient:
    def __init__(self, config):
        self._config = config
        self.service = self._build_service()

    def _build_service(self):
        creds = None
        token_path = Path(self._config.gmail_token_file)
        creds_path = Path(self._config.gmail_credentials_file)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not creds_path.exists():
                    raise FileNotFoundError(
                        f"Gmail credentials not found at '{creds_path}'. "
                        "Run setup_gmail_auth.py first or set GMAIL_CREDENTIALS_FILE."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
                creds = flow.run_local_server(port=0)

            with open(token_path, "w") as f:
                f.write(creds.to_json())
            logger.info("Gmail token saved to %s", token_path)

        return build("gmail", "v1", credentials=creds)

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def get_new_messages(self, start_history_id: str) -> List[Dict]:
        """Return inbox messages added since start_history_id (incremental)."""
        messages = []
        page_token = None
        try:
            while True:
                kwargs: dict = {
                    "userId": "me",
                    "startHistoryId": start_history_id,
                    "historyTypes": ["messageAdded"],
                    "labelId": "INBOX",
                }
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = self.service.users().history().list(**kwargs).execute()

                for record in resp.get("history", []):
                    for added in record.get("messagesAdded", []):
                        msg = added.get("message", {})
                        labels = msg.get("labelIds", [])
                        if "INBOX" in labels and "SENT" not in labels:
                            parsed = self._fetch_message(msg["id"])
                            if parsed:
                                messages.append(parsed)

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:
            err = str(exc)
            if "Invalid startHistoryId" in err or "historyId" in err.lower():
                raise InvalidHistoryIdError(err) from exc
            raise

        return messages

    def get_recent_messages(self, days: int = 1) -> List[Dict]:
        """Return inbox messages received in the last N days."""
        after = (datetime.utcnow() - timedelta(days=days)).strftime("%Y/%m/%d")
        query = f"in:inbox -in:sent after:{after}"
        messages = []
        page_token = None

        while True:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self.service.users().messages().list(**kwargs).execute()

            for ref in resp.get("messages", []):
                parsed = self._fetch_message(ref["id"])
                if parsed:
                    messages.append(parsed)

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return messages

    def _fetch_message(self, message_id: str) -> Optional[Dict]:
        try:
            msg = self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            return self._parse(msg)
        except Exception as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
            return None

    @staticmethod
    def _parse(msg: dict) -> Dict:
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        name, addr = email.utils.parseaddr(raw_from)
        return {
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "sender_raw": raw_from,
            "sender_name": name.strip(),
            "sender_email": addr.lower().strip() if addr else "",
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
        }
