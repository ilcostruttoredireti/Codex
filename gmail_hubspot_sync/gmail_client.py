"""
Gmail client: OAuth2 authentication, incremental message polling via History API.

Flow:
  1. First run  → list all INBOX messages from the last 24h to get a baseline historyId.
  2. Subsequent runs → call users.history.list with the saved historyId to get only new messages.
  3. For each new message fetch headers (From, Subject, Date) and return SenderInfo objects.
"""
from __future__ import annotations

import email.utils
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    raw_name: str
    domain: str
    company: str            # derived from domain
    message_id: str         # Gmail message ID
    subject: str
    date: str


def _parse_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' → ('First', 'Last'). Handles single names and empty strings."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _domain_to_company(domain: str) -> str:
    """Convert 'acme.com' → 'Acme', 'my-startup.io' → 'My Startup'."""
    base = domain.split(".")[0]
    return re.sub(r"[-_]", " ", base).title()


class GmailClient:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._service = None
        self._state_path = Path(config.state_file)
        self._state: dict = self._load_state()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        creds: Credentials | None = None
        token_path = Path(self._config.gmail_token_file)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), self._config.gmail_scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._config.gmail_credentials_file,
                    self._config.gmail_scopes,
                )
                creds = flow.run_local_server(port=0)

            token_path.write_text(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authenticated successfully.")

    # ------------------------------------------------------------------
    # State persistence (history ID)
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_state(self) -> None:
        self._state_path.write_text(json.dumps(self._state, indent=2))

    # ------------------------------------------------------------------
    # Message fetching
    # ------------------------------------------------------------------

    def _get_header(self, headers: list[dict], name: str) -> str:
        for h in headers:
            if h["name"].lower() == name.lower():
                return h["value"]
        return ""

    def _parse_message(self, msg_id: str) -> SenderInfo | None:
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError as exc:
            logger.warning("Could not fetch message %s: %s", msg_id, exc)
            return None

        headers = msg.get("payload", {}).get("headers", [])
        from_header = self._get_header(headers, "From")
        subject = self._get_header(headers, "Subject")
        date = self._get_header(headers, "Date")

        if not from_header:
            return None

        # Parse "Display Name <user@example.com>" or bare "user@example.com"
        realname, addr = email.utils.parseaddr(from_header)
        addr = addr.lower().strip()
        if not addr or "@" not in addr:
            return None

        domain = addr.split("@")[1]
        first, last = _parse_name(realname)

        return SenderInfo(
            email=addr,
            first_name=first,
            last_name=last,
            raw_name=realname,
            domain=domain,
            company=_domain_to_company(domain),
            message_id=msg_id,
            subject=subject,
            date=date,
        )

    # ------------------------------------------------------------------
    # Polling logic
    # ------------------------------------------------------------------

    def _bootstrap_history_id(self) -> str:
        """Fetch recent INBOX messages and record the current historyId."""
        result = (
            self._service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=100, q="newer_than:1d")
            .execute()
        )
        # Get the profile historyId (always up-to-date)
        profile = self._service.users().getProfile(userId="me").execute()
        history_id = profile["historyId"]
        logger.info("Bootstrap historyId: %s", history_id)
        return history_id

    def poll_new_senders(self) -> Iterator[SenderInfo]:
        """
        Yield SenderInfo for every new inbound email since the last poll.
        Skips ignored domains and already-seen message IDs.
        """
        if self._service is None:
            raise RuntimeError("Call authenticate() before polling.")

        history_id: str | None = self._state.get("history_id")

        if not history_id:
            # First run: establish baseline without yielding old messages
            self._state["history_id"] = self._bootstrap_history_id()
            self._state.setdefault("seen_ids", [])
            self._save_state()
            logger.info("First run: baseline set. Next poll will capture new emails.")
            return

        seen_ids: set[str] = set(self._state.get("seen_ids", []))
        new_message_ids: list[str] = []
        new_history_id = history_id

        try:
            page_token = None
            while True:
                kwargs = dict(
                    userId="me",
                    startHistoryId=history_id,
                    labelId="INBOX",
                    historyTypes=["messageAdded"],
                )
                if page_token:
                    kwargs["pageToken"] = page_token

                resp = self._service.users().history().list(**kwargs).execute()
                new_history_id = resp.get("historyId", new_history_id)

                for record in resp.get("history", []):
                    for added in record.get("messagesAdded", []):
                        msg = added.get("message", {})
                        msg_id = msg.get("id", "")
                        labels = msg.get("labelIds", [])
                        # Only INBOX messages (excludes Sent, Drafts, etc.)
                        if "INBOX" in labels and msg_id and msg_id not in seen_ids:
                            new_message_ids.append(msg_id)

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId expired — reset
                logger.warning("historyId expired, resetting baseline.")
                self._state["history_id"] = self._bootstrap_history_id()
                self._state["seen_ids"] = []
                self._save_state()
                return
            raise

        # Update state before yielding (so a crash mid-yield doesn't reprocess)
        self._state["history_id"] = new_history_id
        seen_ids.update(new_message_ids)
        # Keep seen_ids bounded to avoid unbounded growth (last 5000 IDs)
        self._state["seen_ids"] = list(seen_ids)[-5000:]
        self._save_state()

        for msg_id in new_message_ids:
            sender = self._parse_message(msg_id)
            if sender is None:
                continue
            if sender.domain in self._config.ignored_domains:
                logger.debug("Skipping ignored domain: %s", sender.domain)
                continue
            yield sender
