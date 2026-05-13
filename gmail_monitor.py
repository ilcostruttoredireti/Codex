"""
Gmail monitor: authenticates via OAuth2, polls for new messages,
and yields parsed sender records.
"""

import base64
import email as email_lib
import json
import logging
import os
import re
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

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

# Domains whose emails should be skipped (no-reply senders, mailing lists, etc.)
_SKIP_DOMAINS = {
    "noreply.github.com",
    "notifications.github.com",
    "mail.gmail.com",
    "bounce.em.quora.com",
}

_NOREPLY_PATTERN = re.compile(
    r"^(no[-_]?reply|noreply|donotreply|bounce|mailer-daemon|postmaster)@",
    re.IGNORECASE,
)


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    full_name: str
    domain: str
    company: str          # derived from domain
    message_id: str       # Gmail message ID
    thread_id: str
    subject: str
    received_at: str      # RFC 2822 date string


def _get_credentials(credentials_file: str, token_file: str) -> Credentials:
    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())

    return creds


def _build_service(credentials_file: str, token_file: str):
    creds = _get_credentials(credentials_file, token_file)
    return build("gmail", "v1", credentials=creds)


def _parse_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Handles one-word names."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def _company_from_domain(domain: str) -> str:
    """
    Best-effort company name from domain.
    'acme.com' → 'Acme', 'mail.google.com' → 'Google'
    """
    # Strip common prefixes
    for prefix in ("mail.", "email.", "smtp.", "mx.", "info.", "hello."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
            break

    # Drop TLD(s)
    base = domain.split(".")[0]
    return base.capitalize()


def _is_skippable(sender_email: str, domain: str) -> bool:
    if domain in _SKIP_DOMAINS:
        return True
    if _NOREPLY_PATTERN.match(sender_email):
        return True
    return False


def _extract_sender(headers: list[dict]) -> tuple[str, str]:
    """Return (display_name, email_address) from message headers."""
    for h in headers:
        if h["name"].lower() == "from":
            raw = h["value"]
            # "Display Name <email@domain.com>"
            match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>', raw)
            if match:
                return match.group(1).strip(), match.group(2).strip().lower()
            # plain email only
            match = re.match(r'^([^\s@]+@[^\s@]+\.[^\s@]+)$', raw.strip())
            if match:
                return "", match.group(1).lower()
            return "", raw.strip().lower()
    return "", ""


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _parse_message(msg: dict) -> SenderInfo | None:
    headers = msg.get("payload", {}).get("headers", [])
    display_name, sender_email = _extract_sender(headers)

    if not sender_email or "@" not in sender_email:
        return None

    domain = sender_email.split("@")[1]

    if _is_skippable(sender_email, domain):
        logger.debug("Skipping no-reply/bounce sender: %s", sender_email)
        return None

    first, last = _parse_name(display_name) if display_name else ("", "")
    company = _company_from_domain(domain)
    subject = _get_header(headers, "Subject")
    received_at = _get_header(headers, "Date")

    return SenderInfo(
        email=sender_email,
        first_name=first,
        last_name=last,
        full_name=display_name,
        domain=domain,
        company=company,
        message_id=msg["id"],
        thread_id=msg.get("threadId", ""),
        subject=subject,
        received_at=received_at,
    )


class GmailMonitor:
    """
    Polls Gmail for new messages matching a query and yields SenderInfo records.

    Uses a local state file to track the last processed historyId so no
    message is processed twice across restarts.
    """

    STATE_FILE = ".gmail_sync_state.json"

    def __init__(
        self,
        credentials_file: str,
        token_file: str,
        query: str = "label:inbox is:unread",
        poll_interval: int = 60,
    ):
        self.query = query
        self.poll_interval = poll_interval
        self._service = _build_service(credentials_file, token_file)
        self._processed_ids: set[str] = self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> set[str]:
        if Path(self.STATE_FILE).exists():
            data = json.loads(Path(self.STATE_FILE).read_text())
            return set(data.get("processed_ids", []))
        return set()

    def _save_state(self) -> None:
        Path(self.STATE_FILE).write_text(
            json.dumps({"processed_ids": list(self._processed_ids)}, indent=2)
        )

    # ------------------------------------------------------------------
    # Core polling
    # ------------------------------------------------------------------

    def _fetch_message_ids(self) -> list[str]:
        try:
            result = (
                self._service.users()
                .messages()
                .list(userId="me", q=self.query, maxResults=100)
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
            logger.error("Gmail get message %s error: %s", msg_id, exc)
            return None

    def poll_once(self) -> Generator[SenderInfo, None, None]:
        """Fetch new messages and yield SenderInfo for unprocessed ones."""
        ids = self._fetch_message_ids()
        new_ids = [i for i in ids if i not in self._processed_ids]
        logger.info("Found %d new message(s) to process.", len(new_ids))

        for msg_id in new_ids:
            msg = self._fetch_message(msg_id)
            if msg is None:
                continue
            info = _parse_message(msg)
            self._processed_ids.add(msg_id)
            if info:
                yield info

        self._save_state()

    def run_forever(self) -> Generator[SenderInfo, None, None]:
        """Continuously poll Gmail, yielding new SenderInfo records."""
        logger.info(
            "Starting Gmail monitor | query=%r | interval=%ds",
            self.query,
            self.poll_interval,
        )
        while True:
            yield from self.poll_once()
            logger.debug("Sleeping %ds before next poll.", self.poll_interval)
            time.sleep(self.poll_interval)
