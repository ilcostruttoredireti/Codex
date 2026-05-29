"""Gmail API client — read-only access to inbox messages."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

# Read-only inbox access; add gmail.modify if you want to apply labels
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


@dataclass
class EmailMessage:
    message_id: str
    thread_id: str
    from_header: str
    subject: str
    date: datetime
    snippet: str


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = self._build_service()
        self._inbox_label_id: Optional[str] = None

    def _build_service(self):
        creds = self._load_credentials()
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _load_credentials(self) -> Credentials:
        import os
        from pathlib import Path

        creds: Optional[Credentials] = None
        token_path = Path(self._token_file)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        return creds

    def list_inbox_messages(
        self, after: Optional[datetime] = None, max_results: int = 100
    ) -> list[EmailMessage]:
        """Fetch inbox messages newer than `after`, most recent first."""
        query = "in:inbox -in:spam -in:trash"
        if after:
            # Gmail uses Unix epoch for 'after:' date filter (seconds)
            epoch = int(after.timestamp())
            query += f" after:{epoch}"

        messages: list[EmailMessage] = []
        page_token: Optional[str] = None

        while True:
            try:
                kwargs: dict = {
                    "userId": "me",
                    "q": query,
                    "maxResults": min(max_results - len(messages), 100),
                    "labelIds": ["INBOX"],
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                response = self._service.users().messages().list(**kwargs).execute()
            except HttpError as e:
                log.error("Gmail list error: %s", e)
                _backoff(e)
                continue

            for item in response.get("messages", []):
                msg = self._get_message(item["id"])
                if msg:
                    messages.append(msg)

            page_token = response.get("nextPageToken")
            if not page_token or len(messages) >= max_results:
                break

        return messages

    def _get_message(self, message_id: str) -> Optional[EmailMessage]:
        """Fetch a single message's headers."""
        for attempt in range(3):
            try:
                msg = (
                    self._service.users()
                    .messages()
                    .get(userId="me", id=message_id, format="metadata",
                         metadataHeaders=["From", "Subject", "Date"])
                    .execute()
                )
                return self._parse_message(msg)
            except HttpError as e:
                if e.resp.status == 429:
                    time.sleep(2 ** attempt)
                else:
                    log.warning("Could not fetch message %s: %s", message_id, e)
                    return None
        return None

    def _parse_message(self, raw: dict) -> EmailMessage:
        headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])}
        date_str = headers.get("date", "")
        date = _parse_date(date_str)

        return EmailMessage(
            message_id=raw["id"],
            thread_id=raw.get("threadId", ""),
            from_header=headers.get("from", ""),
            subject=headers.get("subject", ""),
            date=date,
            snippet=raw.get("snippet", ""),
        )

    def add_label(self, message_id: str, label_name: str) -> None:
        """Create label if needed and apply it to a message."""
        label_id = self._get_or_create_label(label_name)
        if not label_id:
            return
        try:
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except HttpError as e:
            log.warning("Could not label message %s: %s", message_id, e)

    def _get_or_create_label(self, name: str) -> Optional[str]:
        try:
            labels = self._service.users().labels().list(userId="me").execute()
            for lbl in labels.get("labels", []):
                if lbl["name"].lower() == name.lower():
                    return lbl["id"]
            # Create it
            result = (
                self._service.users()
                .labels()
                .create(userId="me", body={"name": name})
                .execute()
            )
            return result["id"]
        except HttpError as e:
            log.warning("Could not get/create label '%s': %s", name, e)
            return None


def _parse_date(date_str: str) -> datetime:
    """Best-effort parse of an RFC 2822 date string."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _backoff(error: HttpError, base: float = 2.0) -> None:
    if error.resp.status == 429:
        time.sleep(base)
    elif error.resp.status >= 500:
        time.sleep(base * 2)
