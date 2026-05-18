"""Gmail API client: authentication and message fetching."""

import email.utils
import logging
import os
from typing import Optional

from .models import SenderInfo
from .config import GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_LABEL_FILTER

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# ---------------------------------------------------------------------------
# Pure helpers (no Google imports needed here)
# ---------------------------------------------------------------------------

def _parse_name(display_name: str) -> tuple[Optional[str], Optional[str]]:
    """Split 'First Last' into (first, last). Returns (None, None) if empty."""
    parts = display_name.strip().split(None, 1)
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


def _company_from_domain(domain: str) -> Optional[str]:
    """Derive a human-readable company name from an email domain."""
    personal_domains = {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
        "hotmail.com", "hotmail.it", "outlook.com", "live.com",
        "icloud.com", "me.com", "libero.it", "virgilio.it",
        "tiscali.it", "tin.it", "fastwebnet.it",
    }
    if domain.lower() in personal_domains:
        return None
    # Take leftmost label (works for both acme.com and my-corp.co.uk)
    name_part = domain.split(".")[0]
    return name_part.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Gmail service builder (imports deferred to avoid startup cost / test issues)
# ---------------------------------------------------------------------------

def _build_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds: Optional[Credentials] = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class GmailClient:
    def __init__(self):
        self._service = _build_service()

    def fetch_new_messages(self, since_history_id: Optional[str] = None,
                           max_results: int = 100) -> tuple[list[SenderInfo], str]:
        """
        Return (senders, latest_history_id).

        First call (since_history_id is None) fetches the last max_results messages.
        Subsequent calls use the incremental history API.
        """
        label_ids = [GMAIL_LABEL_FILTER] if GMAIL_LABEL_FILTER else ["INBOX"]

        if since_history_id:
            return self._fetch_via_history(since_history_id, label_ids)
        return self._fetch_recent(label_ids, max_results)

    def _fetch_recent(self, label_ids: list[str],
                      max_results: int) -> tuple[list[SenderInfo], str]:
        from googleapiclient.errors import HttpError
        try:
            result = (
                self._service.users()
                .messages()
                .list(userId="me", labelIds=label_ids, maxResults=max_results)
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail list error: %s", exc)
            return [], ""

        messages = result.get("messages", [])
        senders = [s for msg in messages if (s := self._extract_sender(msg["id"])) is not None]
        latest_history_id = self._current_history_id()
        return senders, latest_history_id

    def _fetch_via_history(self, since_history_id: str,
                           label_ids: list[str]) -> tuple[list[SenderInfo], str]:
        from googleapiclient.errors import HttpError
        try:
            result = (
                self._service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=since_history_id,
                    historyTypes=["messageAdded"],
                    labelId=label_ids[0],
                )
                .execute()
            )
        except HttpError as exc:
            logger.warning("Gmail history error (falling back to full fetch): %s", exc)
            return self._fetch_recent(label_ids, 20)

        new_history_id: str = result.get("historyId", since_history_id)
        message_ids: list[str] = [
            added["message"]["id"]
            for record in result.get("history", [])
            for added in record.get("messagesAdded", [])
        ]
        senders = [s for mid in message_ids if (s := self._extract_sender(mid)) is not None]
        return senders, new_history_id

    def _current_history_id(self) -> str:
        profile = self._service.users().getProfile(userId="me").execute()
        return profile.get("historyId", "")

    def _extract_sender(self, message_id: str) -> Optional[SenderInfo]:
        from googleapiclient.errors import HttpError
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
        except HttpError as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
            return None

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        subject = headers.get("Subject", "")

        try:
            display_name, addr_str = email.utils.parseaddr(raw_from)
        except Exception:
            return None

        addr_str = addr_str.strip().lower()
        if not addr_str or "@" not in addr_str:
            return None

        domain = addr_str.split("@", 1)[1]
        first_name, last_name = _parse_name(display_name)
        company = _company_from_domain(domain)

        return SenderInfo(
            email=addr_str,
            first_name=first_name,
            last_name=last_name,
            domain=domain,
            company=company,
            message_id=message_id,
            subject=subject,
        )
