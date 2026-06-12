"""Gmail API client with OAuth2 and message pagination."""

import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


def get_service(credentials_file: str = "credentials.json", token_file: str = "token.json"):
    creds = None
    token_path = Path(token_file)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
        log.info("Token Gmail salvato in %s", token_file)

    return build("gmail", "v1", credentials=creds)


def list_inbox_messages(service, after_timestamp: int | None = None, max_results: int = 50) -> list[dict]:
    """Return messages from inbox, optionally filtered by Unix timestamp."""
    query_parts = ["in:inbox", "-from:me", "-is:draft"]
    if after_timestamp:
        query_parts.append(f"after:{after_timestamp}")

    query = " ".join(query_parts)
    messages: list[dict] = []
    page_token = None

    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": min(max_results - len(messages), 50)}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**kwargs).execute()
        batch = resp.get("messages", [])
        messages.extend(batch)

        page_token = resp.get("nextPageToken")
        if not page_token or len(messages) >= max_results:
            break

    return messages


def get_message_headers(service, msg_id: str) -> dict:
    """Return parsed headers (From, Date, Subject) and threadId for a message."""
    msg = service.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From", "Date", "Subject"],
    ).execute()

    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    return {
        "id": msg_id,
        "thread_id": msg.get("threadId"),
        "from": headers.get("From", ""),
        "date": headers.get("Date", ""),
        "subject": headers.get("Subject", ""),
        "internal_date": int(msg.get("internalDate", 0)) // 1000,
    }


def add_label_to_thread(service, thread_id: str, label_id: str) -> None:
    service.users().threads().modify(
        userId="me",
        id=thread_id,
        body={"addLabelIds": [label_id]},
    ).execute()


def get_or_create_label(service, name: str) -> str:
    """Return label ID, creating the label if it doesn't exist."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"].lower() == name.lower():
            return lbl["id"]

    result = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    log.info("Label Gmail creata: %s (%s)", name, result["id"])
    return result["id"]
