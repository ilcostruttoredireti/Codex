"""Gmail API client for fetching and parsing incoming emails."""

import base64
import email
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Read-only access to Gmail messages and metadata
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class SenderInfo:
    email: str
    full_name: str | None
    first_name: str | None
    last_name: str | None
    domain: str
    message_id: str
    subject: str
    received_at: int  # Unix timestamp ms


def _parse_name(full_name: str | None) -> tuple[str | None, str | None]:
    """Split 'First Last' into (first, last). Handles multi-word last names."""
    if not full_name:
        return None, None
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def _extract_email_and_name(from_header: str) -> tuple[str, str | None]:
    """Parse 'Display Name <addr@example.com>' or 'addr@example.com'."""
    from_header = from_header.strip()
    if "<" in from_header and ">" in from_header:
        name_part = from_header[: from_header.index("<")].strip().strip('"')
        addr_part = from_header[from_header.index("<") + 1 : from_header.index(">")].strip()
        return addr_part.lower(), name_part or None
    return from_header.lower(), None


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None

    def authenticate(self) -> None:
        """Run OAuth2 flow (opens browser on first run, uses cached token after)."""
        creds = None
        if Path(self._token_file).exists():
            creds = Credentials.from_authorized_user_file(self._token_file, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "w") as fh:
                fh.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authentication successful")

    def _list_message_ids(self, query: str, max_results: int = 100) -> list[str]:
        """Return a list of message IDs matching the Gmail search query."""
        try:
            result = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            return [m["id"] for m in result.get("messages", [])]
        except HttpError as exc:
            logger.error("Gmail list error: %s", exc)
            return []

    def _fetch_message(self, msg_id: str) -> dict | None:
        try:
            return (
                self._service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail fetch error for %s: %s", msg_id, exc)
            return None

    def _parse_sender(self, raw_msg: dict) -> SenderInfo | None:
        headers = {h["name"]: h["value"] for h in raw_msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "")
        internal_date = int(raw_msg.get("internalDate", 0))

        if not from_header:
            return None

        addr, full_name = _extract_email_and_name(from_header)
        if not addr or "@" not in addr:
            return None

        domain = addr.split("@")[1]

        # Skip no-reply / automated senders
        local = addr.split("@")[0]
        if any(kw in local for kw in ("noreply", "no-reply", "mailer-daemon", "postmaster")):
            logger.debug("Skipping automated sender: %s", addr)
            return None

        first, last = _parse_name(full_name)

        return SenderInfo(
            email=addr,
            full_name=full_name,
            first_name=first,
            last_name=last,
            domain=domain,
            message_id=raw_msg["id"],
            subject=subject,
            received_at=internal_date,
        )

    def fetch_new_senders(
        self, after_epoch_ms: int, seen_message_ids: set[str]
    ) -> Generator[SenderInfo, None, None]:
        """Yield SenderInfo for each unseen inbox message received after `after_epoch_ms`."""
        after_sec = after_epoch_ms // 1000
        query = f"in:inbox after:{after_sec}"
        msg_ids = self._list_message_ids(query)

        for msg_id in msg_ids:
            if msg_id in seen_message_ids:
                continue
            raw = self._fetch_message(msg_id)
            if raw is None:
                continue
            sender = self._parse_sender(raw)
            if sender:
                yield sender
