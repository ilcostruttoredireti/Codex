import json
import logging
import os
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES, GMAIL_TOKEN_FILE

logger = logging.getLogger(__name__)

_PROCESSED_IDS_FILE = "processed_message_ids.json"


class GmailMonitor:
    def __init__(self):
        self.service = _authenticate()
        self._processed_ids: set[str] = _load_ids()

    def new_inbox_messages(self, max_results: int = 50) -> Iterator[dict]:
        """Yield full message metadata for each unprocessed inbox message."""
        try:
            result = (
                self.service.users()
                .messages()
                .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail list error: %s", exc)
            return

        for ref in result.get("messages", []):
            msg_id = ref["id"]
            if msg_id in self._processed_ids:
                continue
            try:
                message = (
                    self.service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=msg_id,
                        format="metadata",
                        metadataHeaders=["From", "Reply-To", "Subject", "Date"],
                    )
                    .execute()
                )
                self._processed_ids.add(msg_id)
                yield message
            except HttpError as exc:
                logger.error("Gmail get message %s error: %s", msg_id, exc)

        _save_ids(self._processed_ids)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _authenticate():
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
        with open(GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _load_ids() -> set:
    if os.path.exists(_PROCESSED_IDS_FILE):
        with open(_PROCESSED_IDS_FILE) as fh:
            return set(json.load(fh))
    return set()


def _save_ids(ids: set) -> None:
    with open(_PROCESSED_IDS_FILE, "w") as fh:
        json.dump(list(ids), fh)
