"""
Gmail API client: authenticate and fetch recent inbox messages.
"""
import os
import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_SCOPES, LOOKBACK_DAYS

logger = logging.getLogger(__name__)


def _get_credentials() -> Credentials:
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
    return creds


def build_service():
    return build("gmail", "v1", credentials=_get_credentials())


def fetch_recent_messages(service, days: int = LOOKBACK_DAYS) -> Generator[dict, None, None]:
    """
    Yield message dicts (id, threadId, snippet, payload.headers) for inbox messages
    received within the past `days` days, excluding sent/drafts/spam.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    after_ts = int(cutoff.timestamp())
    query = f"in:inbox -from:me after:{after_ts}"

    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**kwargs).execute()
        messages = resp.get("messages", [])
        logger.info("Fetched page: %d messages", len(messages))

        for msg_stub in messages:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=msg_stub["id"], format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
            yield full

        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def get_message_snippet(service, message_id: str) -> str:
    """Return the plaintext snippet for a message (for forward detection)."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    return msg.get("snippet", "")


def extract_header(message: dict, name: str) -> str:
    """Return the value of a header from a Gmail message dict."""
    headers = message.get("payload", {}).get("headers", [])
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""
