import logging
import os
import re
from email.utils import parseaddr
from typing import TYPE_CHECKING, List, Optional

from .models import EmailSender

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
]

NO_REPLY_PATTERN = re.compile(
    r"^(no[-_]?reply|noreply|donotreply|do[-_]not[-_]reply|postmaster|mailer-daemon|bounce)",
    re.IGNORECASE,
)


def _build_gmail_service(credentials_file: str, token_file: str):
    """Build an authenticated Gmail API service. Imports google libs lazily."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_file):
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {credentials_file}\n"
                    "Download it from Google Cloud Console → APIs & Credentials → "
                    "OAuth 2.0 Client IDs, then save it as credentials.json."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._service = None  # lazy — avoids import at module load time

    @property
    def service(self):
        if self._service is None:
            self._service = _build_gmail_service(self.credentials_file, self.token_file)
        return self._service

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def get_new_messages_since(self, history_id: str) -> List[dict]:
        """Return inbox messages added since the given historyId."""
        from googleapiclient.errors import HttpError

        messages: List[dict] = []
        try:
            page_token = None
            while True:
                kwargs: dict = dict(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                if page_token:
                    kwargs["pageToken"] = page_token

                response = self.service.users().history().list(**kwargs).execute()

                for record in response.get("history", []):
                    for msg_added in record.get("messagesAdded", []):
                        msg = msg_added.get("message", {})
                        labels = msg.get("labelIds", [])
                        if "INBOX" in labels and "SENT" not in labels:
                            messages.append(msg)

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as e:
            if e.resp.status == 404:
                logger.warning("historyId expired — falling back to recent inbox messages")
                return self.get_recent_inbox_messages(max_results=50)
            raise

        return messages

    def get_recent_inbox_messages(self, max_results: int = 50) -> List[dict]:
        response = self.service.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=max_results,
        ).execute()
        return response.get("messages", [])

    def get_message_sender(self, message_id: str) -> Optional[EmailSender]:
        from googleapiclient.errors import HttpError

        try:
            msg = self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except HttpError:
            return None

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        from_header = headers.get("From", "").strip()
        if not from_header:
            return None

        name, email_addr = parseaddr(from_header)
        if not email_addr or "@" not in email_addr:
            return None

        email_addr = email_addr.lower().strip()
        local, domain = email_addr.split("@", 1)

        if NO_REPLY_PATTERN.match(local):
            return None

        return EmailSender(
            email=email_addr,
            name=name.strip() or None,
            domain=domain,
            message_id=message_id,
            subject=headers.get("Subject", "(nessun oggetto)"),
            date=headers.get("Date", ""),
        )
