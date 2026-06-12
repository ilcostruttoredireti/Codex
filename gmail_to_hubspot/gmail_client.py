"""Gmail API client — wraps google-api-python-client."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Generator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.modify"]

TOKEN_PATH = Path(os.getenv("GMAIL_TOKEN_PATH", "token.json"))
CREDS_PATH = Path(os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json"))


def _get_credentials() -> Credentials:
    creds: Optional[Credentials] = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def get_service():
    return build("gmail", "v1", credentials=_get_credentials())


def iter_inbox_messages(service, max_results: int = 50, after_history_id: int = 0) -> Generator[dict, None, None]:
    """Yield message dicts (id, threadId, from, subject) from INBOX."""
    query = "in:inbox -in:sent -from:me"
    kwargs: dict = {"userId": "me", "labelIds": ["INBOX"], "q": query,
                    "maxResults": max_results}
    response = service.users().messages().list(**kwargs).execute()
    messages = response.get("messages", [])

    for msg_stub in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_stub["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"]
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        yield {
            "id": msg["id"],
            "threadId": msg["threadId"],
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", ""),
        }


def add_label(service, message_id: str, label_name: str) -> None:
    """Apply a Gmail label to a message, creating it first if needed."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    label_id = next((lb["id"] for lb in labels if lb["name"] == label_name), None)
    if not label_id:
        body = {"name": label_name, "labelListVisibility": "labelShow",
                "messageListVisibility": "show"}
        label_id = service.users().labels().create(userId="me", body=body).execute()["id"]
    service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id]}
    ).execute()
