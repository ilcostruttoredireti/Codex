"""Thin wrapper around the Gmail API for reading inbox messages."""

import os
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")


def _get_service():
    creds = None
    if os.path.exists(_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_inbox_messages(
    max_results: int = 100,
    query: str = "in:inbox -from:me",
    page_token: str | None = None,
) -> tuple[list[dict], str | None]:
    """
    Return (messages, next_page_token).
    Each message dict has 'id' and 'threadId'.
    """
    svc = _get_service()
    params: dict = {"userId": "me", "maxResults": max_results, "q": query, "labelIds": ["INBOX"]}
    if page_token:
        params["pageToken"] = page_token
    resp = svc.users().messages().list(**params).execute()
    return resp.get("messages", []), resp.get("nextPageToken")


def get_from_header(message_id: str) -> str:
    """Return the raw From header value for a single message (minimal fetch)."""
    svc = _get_service()
    msg = svc.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From"],
    ).execute()
    for header in msg.get("payload", {}).get("headers", []):
        if header["name"].lower() == "from":
            return header["value"]
    return ""


def iter_inbox_senders(
    max_results: int = 100,
    query: str = "in:inbox -from:me",
) -> Iterator[str]:
    """Yield raw From header strings for all inbox messages matching query."""
    page_token = None
    fetched = 0
    while True:
        messages, page_token = list_inbox_messages(
            max_results=min(max_results - fetched, 100),
            query=query,
            page_token=page_token,
        )
        for msg in messages:
            from_header = get_from_header(msg["id"])
            if from_header:
                yield from_header
            fetched += 1
            if fetched >= max_results:
                return
        if not page_token or fetched >= max_results:
            break
