"""Thin wrapper around the Gmail API (v1)."""

import logging
import os
from pathlib import Path
from typing import List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_TOKEN_FILE = Path(__file__).parent / "token.json"
_CREDS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", str(Path(__file__).parent / "credentials.json")))


def get_service():
    """Authenticate (OAuth2) and return a Gmail API service object.

    On the first run this opens a browser window for the consent flow and
    writes token.json; subsequent runs reuse / refresh the saved token.
    """
    creds: Optional[Credentials] = None

    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not _CREDS_FILE.exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {_CREDS_FILE}\n"
                    "Download it from Google Cloud Console → APIs → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(_CREDS_FILE), _SCOPES)
            creds = flow.run_local_server(port=0)

        _TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_inbox_message_ids(
    service,
    after_history_id: Optional[str] = None,
    max_results: int = 100,
) -> List[str]:
    """Return a list of inbox message IDs to process.

    Uses the History API for incremental fetches (after_history_id set) and
    falls back to a full list when no cursor exists or the history has expired.
    """
    if after_history_id:
        try:
            response = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=after_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            ids: List[str] = []
            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []):
                        ids.append(msg["id"])
            return ids
        except HttpError as exc:
            if exc.status_code == 404:
                logger.warning("Gmail history ID expired — falling back to full fetch")
                return get_inbox_message_ids(service, max_results=max_results)
            raise

    # Initial full fetch: grab the most recent messages in the inbox
    result = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=max_results, q="-from:me")
        .execute()
    )
    return [m["id"] for m in result.get("messages", [])]


def get_from_header(service, message_id: str) -> Optional[str]:
    """Return just the From: header value for a single message (minimal API call)."""
    try:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From"],
            )
            .execute()
        )
        for header in msg.get("payload", {}).get("headers", []):
            if header["name"].lower() == "from":
                return header["value"]
    except HttpError as exc:
        logger.warning("Failed to fetch message %s: %s", message_id, exc)
    return None


def get_profile_history_id(service) -> str:
    """Return the current mailbox historyId (used to set the cursor after each cycle)."""
    profile = service.users().getProfile(userId="me").execute()
    return str(profile["historyId"])
