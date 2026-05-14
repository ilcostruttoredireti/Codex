"""
Gmail API client for monitoring incoming emails.

Authentication uses OAuth2 with offline access. On first run, a browser
window opens for user consent; subsequent runs use the saved token.
"""

from __future__ import annotations

import base64
import email as email_lib
import json
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

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Path for persisting OAuth tokens between runs
TOKEN_PATH = Path(os.getenv("GMAIL_TOKEN_PATH", "token.json"))
CREDENTIALS_PATH = Path(os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json"))


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    full_name: str
    domain: str
    company: str
    message_id: str
    subject: str


def _build_service():
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found at '{CREDENTIALS_PATH}'. "
                    "Download it from Google Cloud Console (OAuth 2.0 client ID)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _parse_from_header(from_header: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a From header value."""
    parsed = email_lib.utils.parseaddr(from_header)
    return parsed[0], parsed[1].lower()


def _split_name(full_name: str) -> tuple[str, str]:
    """Return (first_name, last_name) from a display name string."""
    parts = full_name.strip().split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _company_from_domain(domain: str) -> str:
    """Extract a human-readable company name from an email domain."""
    known_free = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "icloud.com", "live.com", "aol.com", "protonmail.com",
        "mail.com", "gmx.com", "yandex.com", "libero.it",
        "virgilio.it", "alice.it", "tiscali.it",
    }
    if domain in known_free:
        return ""
    # Strip TLD(s) and capitalise
    parts = domain.split(".")
    if len(parts) >= 2:
        name = parts[-2]  # e.g. "acme" from "acme.com"
        return name.replace("-", " ").title()
    return domain.title()


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


class GmailClient:
    """Wrapper around the Gmail API for fetching new inbound messages."""

    def __init__(self) -> None:
        self._service = _build_service()
        self._history_id_file = Path(os.getenv("GMAIL_HISTORY_FILE", ".gmail_history_id"))
        self._last_history_id: str | None = self._load_history_id()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poll_new_senders(self) -> Generator[SenderInfo, None, None]:
        """
        Yield SenderInfo for every new inbound message since the last poll.

        On the very first call, processes the most recent 50 messages so we
        don't flood HubSpot with the entire mailbox history.
        """
        if self._last_history_id is None:
            yield from self._bootstrap_recent()
        else:
            yield from self._fetch_via_history()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _bootstrap_recent(self) -> Generator[SenderInfo, None, None]:
        """First-run: fetch the last 50 messages and record the history ID."""
        try:
            result = (
                self._service.users()
                .messages()
                .list(userId="me", labelIds=["INBOX"], maxResults=50)
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"Gmail API error during bootstrap: {exc}") from exc

        messages = result.get("messages", [])
        for msg_stub in messages:
            info = self._fetch_message(msg_stub["id"])
            if info:
                yield info

        # Record current history ID so next poll uses the incremental API
        profile = self._service.users().getProfile(userId="me").execute()
        self._save_history_id(profile["historyId"])

    def _fetch_via_history(self) -> Generator[SenderInfo, None, None]:
        """Incremental fetch using Gmail History API."""
        try:
            result = (
                self._service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=self._last_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except HttpError as exc:
            # historyId expired → fall back to recent-only bootstrap
            if exc.resp.status == 404:
                self._last_history_id = None
                yield from self._bootstrap_recent()
                return
            raise RuntimeError(f"Gmail History API error: {exc}") from exc

        history_records = result.get("history", [])
        for record in history_records:
            for added in record.get("messagesAdded", []):
                labels = added.get("message", {}).get("labelIds", [])
                # Only process inbox messages (ignore sent, drafts, etc.)
                if "INBOX" not in labels:
                    continue
                msg_id = added["message"]["id"]
                info = self._fetch_message(msg_id)
                if info:
                    yield info

        if "historyId" in result:
            self._save_history_id(result["historyId"])

    def _fetch_message(self, message_id: str) -> SenderInfo | None:
        """Fetch a single message and extract sender info."""
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
        except HttpError:
            return None

        headers = msg.get("payload", {}).get("headers", [])
        from_header = _get_header(headers, "From")
        subject = _get_header(headers, "Subject")

        if not from_header:
            return None

        full_name, email_address = _parse_from_header(from_header)
        if not email_address or "@" not in email_address:
            return None

        domain = email_address.split("@")[1]
        company = _company_from_domain(domain)
        first_name, last_name = _split_name(full_name)

        return SenderInfo(
            email=email_address,
            first_name=first_name,
            last_name=last_name,
            full_name=full_name,
            domain=domain,
            company=company,
            message_id=message_id,
            subject=subject,
        )

    def _load_history_id(self) -> str | None:
        if self._history_id_file.exists():
            return self._history_id_file.read_text().strip() or None
        return None

    def _save_history_id(self, history_id: str) -> None:
        self._history_id_file.write_text(history_id)
        self._last_history_id = history_id
