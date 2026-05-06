import os
import logging
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import state
from config import GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_SCOPES

log = logging.getLogger(__name__)


def get_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds: Optional[Credentials] = None

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


def fetch_new_messages(service) -> list:
    """
    Return a list of new INBOX messages since the last poll.

    On first run, fetches messages from the last 7 days and saves the current
    historyId. On subsequent runs, uses Gmail's history API to retrieve only
    messages added since the previous poll.
    """
    last_history_id = state.get_state("last_history_id")

    if last_history_id is None:
        log.info("No history ID stored — performing initial fetch (last 7 days).")
        return _initial_fetch(service)

    return _incremental_fetch(service, last_history_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _initial_fetch(service) -> list:
    try:
        results = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], q="newer_than:7d", maxResults=500)
            .execute()
        )
        messages = results.get("messages", [])
        _save_current_history_id(service)
        if not messages:
            return []
        return [_get_message_metadata(service, m["id"]) for m in messages]
    except HttpError as exc:
        log.error("Gmail API error during initial fetch: %s", exc)
        return []


def _incremental_fetch(service, last_history_id: str) -> list:
    try:
        history_response = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=last_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            .execute()
        )

        new_history_id = history_response.get("historyId", last_history_id)
        state.set_state("last_history_id", new_history_id)

        message_ids: list[str] = []
        for record in history_response.get("history", []):
            for added in record.get("messagesAdded", []):
                message_ids.append(added["message"]["id"])

        if not message_ids:
            return []

        return [_get_message_metadata(service, mid) for mid in message_ids]

    except HttpError as exc:
        if exc.resp.status == 404:
            # Stored historyId is too old; reset and re-fetch from scratch.
            log.warning("History ID expired. Resetting to initial fetch.")
            state.set_state("last_history_id", None)
            return _initial_fetch(service)
        log.error("Gmail API error during incremental fetch: %s", exc)
        return []


def _get_message_metadata(service, message_id: str) -> dict:
    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )
    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    return {
        "id": message_id,
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
    }


def _save_current_history_id(service) -> None:
    profile = service.users().getProfile(userId="me").execute()
    state.set_state("last_history_id", str(profile["historyId"]))
