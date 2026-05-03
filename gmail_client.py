import logging
import os
from email.utils import parseaddr
from pathlib import Path
from typing import Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from models import EmailContact

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._service = None
        self._user_email: Optional[str] = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        creds: Optional[Credentials] = None

        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, _SCOPES
                )
                creds = flow.run_local_server(port=0)
            Path(self.token_file).write_text(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)

        profile = self._service.users().getProfile(userId="me").execute()
        self._user_email = profile["emailAddress"].lower()
        logger.info(f"Gmail autenticato come {self._user_email}")

    # ------------------------------------------------------------------
    # History-based incremental fetch
    # ------------------------------------------------------------------

    def get_profile_history_id(self) -> str:
        profile = self._service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def get_new_messages(self, start_history_id: str) -> Tuple[list, str]:
        """Return (deduplicated inbox messages, latest historyId).

        Uses the Gmail History API for efficient incremental polling.
        Falls back and resets historyId when the stored ID has expired (404).
        """
        messages: dict[str, dict] = {}  # keyed by messageId to deduplicate
        latest_id = start_history_id
        page_token: Optional[str] = None

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

                response = self._service.users().history().list(**kwargs).execute()

                for record in response.get("history", []):
                    for item in record.get("messagesAdded", []):
                        msg = item.get("message", {})
                        msg_id = msg.get("id")
                        labels = msg.get("labelIds", [])
                        if msg_id and "INBOX" in labels and msg_id not in messages:
                            messages[msg_id] = msg

                latest_id = str(response.get("historyId", latest_id))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId expired (>7 days old). Reset to current position.
                logger.warning("historyId scaduto, reset al profilo corrente")
                latest_id = self.get_profile_history_id()
            else:
                raise

        return list(messages.values()), latest_id

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    def get_message_sender(self, message_id: str) -> Optional[EmailContact]:
        try:
            msg = self._service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
        except HttpError as exc:
            logger.error(f"Errore recupero messaggio {message_id}: {exc}")
            return None

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        from_header = headers.get("From", "").strip()
        if not from_header:
            return None

        display_name, email_addr = parseaddr(from_header)
        if not email_addr or "@" not in email_addr:
            return None

        email_addr = email_addr.lower().strip()

        # Skip emails sent by the authenticated user (e.g. sent-to-self)
        if email_addr == self._user_email:
            return None

        domain = email_addr.split("@")[1]
        first_name, last_name = _split_name(display_name)

        return EmailContact(
            email=email_addr,
            first_name=first_name,
            last_name=last_name,
            domain=domain,
            message_id=message_id,
            subject=headers.get("Subject", "").strip() or None,
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _split_name(raw: str) -> Tuple[Optional[str], Optional[str]]:
    name = raw.strip().strip('"').strip("'").strip()
    if not name:
        return None, None

    # "Rossi, Mario" → first=Mario, last=Rossi
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        last = parts[0].title() if parts[0] else None
        first = parts[1].title() if len(parts) > 1 and parts[1] else None
        return first, last

    parts = name.split()
    first = parts[0].capitalize()
    last = " ".join(parts[1:]).title() if len(parts) > 1 else None
    return first, last
