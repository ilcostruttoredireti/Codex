"""
Gmail API client — fetches unread inbox threads and extracts sender data.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")


@dataclass
class GmailMessage:
    thread_id: str
    message_id: str
    sender_raw: str  # raw "Name <email>" or "email"
    subject: str
    date: str


def build_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_new_senders(
    service,
    query: str = "in:inbox is:unread newer_than:1d -from:me",
    max_results: int = 100,
) -> Generator[GmailMessage, None, None]:
    """Yield GmailMessage for each unread inbox email."""
    response = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    messages = response.get("messages", [])

    for msg_ref in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_ref["id"], format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sender_raw = headers.get("From", "")
        if not sender_raw:
            continue
        yield GmailMessage(
            thread_id=msg.get("threadId", ""),
            message_id=msg["id"],
            sender_raw=sender_raw,
            subject=headers.get("Subject", ""),
            date=headers.get("Date", ""),
        )
