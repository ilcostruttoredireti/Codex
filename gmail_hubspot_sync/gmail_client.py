import os
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import (
    GMAIL_SCOPES,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
    GOOGLE_TOKEN_PATH,
)


def _get_credentials() -> Credentials:
    creds = None
    if os.path.exists(GOOGLE_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, GMAIL_SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds

    client_config = {
        "installed": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uris": [GOOGLE_REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, GMAIL_SCOPES)
    creds = flow.run_local_server(port=8080)
    _save_token(creds)
    return creds


def _save_token(creds: Credentials) -> None:
    with open(GOOGLE_TOKEN_PATH, "w") as f:
        f.write(creds.to_json())


class GmailClient:
    def __init__(self):
        creds = _get_credentials()
        self._service = build("gmail", "v1", credentials=creds)

    def fetch_inbox_messages(self, max_results: int = 50) -> Generator[dict, None, None]:
        """Yield full message dicts from the inbox (newest first)."""
        result = (
            self._service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        for msg_stub in result.get("messages", []):
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=msg_stub["id"], format="full")
                .execute()
            )
            yield msg

    def get_header(self, message: dict, name: str) -> str:
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == name.lower():
                return h["value"]
        return ""

    def get_plain_body(self, message: dict) -> str:
        """Recursively extract the plaintext body from a message payload."""
        return _extract_plain(message.get("payload", {}))


def _extract_plain(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        import base64
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _extract_plain(part)
        if result:
            return result
    return ""
