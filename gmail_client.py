"""
Gmail API client — authentication, inbox polling, and sender extraction.
"""
import logging
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

log = logging.getLogger(__name__)


# ── Authentication ─────────────────────────────────────────────────────────────

def _load_credentials() -> Optional[Credentials]:
    token_path = Path(config.GMAIL_TOKEN_FILE)
    if token_path.exists():
        return Credentials.from_authorized_user_file(
            str(token_path), config.GMAIL_SCOPES
        )
    return None


def _refresh_or_authorize(creds: Optional[Credentials]) -> Credentials:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        return creds

    flow = InstalledAppFlow.from_client_secrets_file(
        config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SCOPES
    )
    return flow.run_local_server(port=0)


def get_gmail_service():
    """Return an authenticated Gmail API service instance."""
    creds = _load_credentials()

    if not creds or not creds.valid:
        creds = _refresh_or_authorize(creds)
        with open(config.GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Message fetching ───────────────────────────────────────────────────────────

def get_current_history_id(service) -> str:
    """Return the current mailbox historyId."""
    profile = service.users().getProfile(userId="me").execute()
    return profile["historyId"]


def fetch_new_message_ids(service, state: dict) -> list[str]:
    """
    Return IDs of inbox messages not yet processed.

    Uses the History API for incremental updates when a historyId is stored,
    falling back to a list of recent messages on the first run or after a
    history expiration (7-day window).
    """
    processed: set[str] = set(state.get("processed_ids", []))
    history_id: Optional[str] = state.get("history_id")
    new_ids: list[str] = []

    if history_id:
        try:
            new_ids, new_history_id = _fetch_via_history(
                service, history_id, processed
            )
            state["history_id"] = new_history_id
            log.debug("History API: %d new message(s)", len(new_ids))
            return new_ids
        except HttpError as exc:
            if exc.resp.status == 404:
                log.warning("History ID expired — falling back to recent messages")
            else:
                raise

    # First run or history expired
    new_ids = _fetch_recent(service, processed)
    state["history_id"] = get_current_history_id(service)
    log.debug("Recent fetch: %d new message(s)", len(new_ids))
    return new_ids


def _fetch_via_history(
    service, history_id: str, processed: set[str]
) -> tuple[list[str], str]:
    """Fetch new INBOX message IDs via the Gmail History API."""
    new_ids: list[str] = []
    latest_history_id = history_id
    page_token = None

    while True:
        kwargs: dict = dict(
            userId="me",
            startHistoryId=history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
        )
        if page_token:
            kwargs["pageToken"] = page_token

        result = service.users().history().list(**kwargs).execute()
        latest_history_id = result.get("historyId", latest_history_id)

        for record in result.get("history", []):
            for msg_added in record.get("messagesAdded", []):
                msg_id = msg_added["message"]["id"]
                if msg_id not in processed:
                    new_ids.append(msg_id)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return new_ids, latest_history_id


def _fetch_recent(service, processed: set[str]) -> list[str]:
    """Fetch up to INITIAL_FETCH_LIMIT recent INBOX messages."""
    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=config.INITIAL_FETCH_LIMIT,
    ).execute()
    return [
        m["id"] for m in result.get("messages", [])
        if m["id"] not in processed
    ]


# ── Sender extraction ──────────────────────────────────────────────────────────

def extract_sender(service, message_id: str) -> Optional[dict]:
    """
    Return a dict with sender info for *message_id*, or None if the sender
    should be skipped (automated mailer, missing email, etc.).
    """
    try:
        msg = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
    except HttpError as exc:
        log.error("Could not fetch message %s: %s", message_id, exc)
        return None

    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }

    raw_from = headers.get("From", "")
    display_name, email = parseaddr(raw_from)

    if not email or "@" not in email:
        log.debug("Skipping message %s — no valid From address", message_id)
        return None

    email = email.lower().strip()
    local, domain = email.split("@", 1)

    if _is_automated(local):
        log.debug("Skipping automated sender: %s", email)
        return None

    return {
        "email": email,
        "name": display_name.strip() or None,
        "domain": domain,
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "message_id": message_id,
    }


def _is_automated(local_part: str) -> bool:
    """Return True if the local part of an address looks automated."""
    local_lower = local_part.lower()
    return any(pat in local_lower for pat in config.IGNORED_LOCAL_PATTERNS)
