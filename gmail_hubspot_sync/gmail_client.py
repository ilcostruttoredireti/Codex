"""Gmail API client with OAuth2 authentication."""

import os
import json
import time
from pathlib import Path
from typing import Generator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
_CREDS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))


def _get_credentials() -> Credentials:
    creds: Optional[Credentials] = None
    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(_CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        _TOKEN_FILE.write_text(creds.to_json())
    return creds


def build_service():
    return build("gmail", "v1", credentials=_get_credentials())


def list_inbox_threads(
    service,
    query: str = "in:inbox -in:draft",
    page_token: Optional[str] = None,
    max_results: int = 50,
) -> dict:
    return (
        service.users()
        .threads()
        .list(
            userId="me",
            q=query,
            pageToken=page_token,
            maxResults=max_results,
        )
        .execute()
    )


def get_thread_messages(service, thread_id: str) -> list[dict]:
    """Return all messages in a thread with headers and plaintext body."""
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    result = []
    for msg in thread.get("messages", []):
        headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}
        body = _decode_body(msg["payload"])
        result.append(
            {
                "id": msg["id"],
                "threadId": msg["threadId"],
                "labelIds": msg.get("labelIds", []),
                "from": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
                "body": body,
            }
        )
    return result


def _decode_body(payload: dict) -> str:
    import base64

    data = payload.get("body", {}).get("data")
    if data:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        if part.get("mimeType") in ("text/plain", "text/html"):
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    return ""


def iter_new_messages(
    service,
    after_timestamp: Optional[int] = None,
    query: str = "in:inbox -in:draft",
) -> Generator[dict, None, None]:
    """Yield each new message in the inbox since after_timestamp (Unix epoch seconds)."""
    full_query = query
    if after_timestamp:
        full_query += f" after:{after_timestamp}"

    page_token = None
    while True:
        resp = list_inbox_threads(service, query=full_query, page_token=page_token)
        for thread_stub in resp.get("threads", []):
            msgs = get_thread_messages(service, thread_stub["id"])
            for msg in msgs:
                yield msg
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.1)
