"""
Gmail API client — lists new inbox messages since the last processed timestamp.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import Config

logger = logging.getLogger(__name__)


@dataclass
class GmailMessage:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    date: datetime
    snippet: str = ""
    plaintext_body: str = ""
    label_ids: list[str] = field(default_factory=list)


class GmailClient:
    def __init__(self, config: Config):
        self._config = config
        self._service = self._build_service()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _build_service(self):
        creds = None
        token_path = self._config.GMAIL_TOKEN_FILE
        creds_path = self._config.GMAIL_CREDENTIALS_FILE

        if __import__("os").path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, self._config.GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(creds_path, self._config.GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_new_messages(self, since: datetime | None = None) -> list[GmailMessage]:
        """
        Return inbox messages newer than *since* (UTC).
        If *since* is None, fetches the last 50 messages.
        """
        query = "in:inbox -in:draft"
        if since:
            # Gmail uses Unix epoch seconds in after:
            epoch = int(since.timestamp())
            query += f" after:{epoch}"

        messages_raw = self._list_messages(query)
        results: list[GmailMessage] = []

        for item in messages_raw:
            try:
                msg = self._fetch_message(item["id"])
                if msg:
                    results.append(msg)
            except Exception as exc:
                logger.warning("Skipping message %s: %s", item["id"], exc)

        return results

    def list_labels(self) -> list[dict]:
        resp = self._service.users().labels().list(userId="me").execute()
        return resp.get("labels", [])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_messages(self, query: str, max_results: int = 100) -> list[dict]:
        results = []
        page_token = None

        while True:
            kwargs = dict(userId="me", q=query, maxResults=min(max_results - len(results), 100))
            if page_token:
                kwargs["pageToken"] = page_token

            resp = self._service.users().messages().list(**kwargs).execute()
            results.extend(resp.get("messages", []))

            page_token = resp.get("nextPageToken")
            if not page_token or len(results) >= max_results:
                break

        return results

    def _fetch_message(self, msg_id: str) -> GmailMessage | None:
        raw = self._service.users().messages().get(
            userId="me", id=msg_id, format="full",
        ).execute()

        headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])}
        sender = headers.get("from", "")
        subject = headers.get("subject", "")
        date_str = headers.get("date", "")

        date = _parse_date(date_str)
        if date is None:
            return None

        plaintext_body = _extract_plaintext(raw.get("payload", {}))

        return GmailMessage(
            message_id=msg_id,
            thread_id=raw.get("threadId", ""),
            sender=sender,
            subject=subject,
            date=date,
            snippet=raw.get("snippet", ""),
            plaintext_body=plaintext_body,
            label_ids=raw.get("labelIds", []),
        )


def _extract_plaintext(payload: dict) -> str:
    """Recursively extract text/plain content from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text = _extract_plaintext(part)
        if text:
            return text
    return ""


def _parse_date(date_str: str) -> datetime | None:
    """Best-effort RFC 2822 date parser."""
    if not date_str:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:
        return None
