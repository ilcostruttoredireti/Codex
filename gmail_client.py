"""Gmail client: OAuth2 authentication and incremental inbox monitoring."""

import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class SenderInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]       # inferred from domain
    domain: str
    message_id: str
    subject: str
    received_at: Optional[str]


def _parse_name(display_name: str) -> tuple[Optional[str], Optional[str]]:
    """Split 'First Last' into (first, last). Returns (None, None) if blank."""
    display_name = display_name.strip()
    if not display_name:
        return None, None
    parts = display_name.split(maxsplit=1)
    first = parts[0] if parts else None
    last = parts[1] if len(parts) > 1 else None
    return first, last


def _company_from_domain(domain: str) -> Optional[str]:
    """
    Convert a domain like 'acme.com' → 'Acme'.
    Skips generic free-mail providers.
    """
    free_providers = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "icloud.com", "live.com", "msn.com", "aol.com",
        "protonmail.com", "proton.me", "tutanota.com",
        "libero.it", "virgilio.it", "tiscali.it",
    }
    if domain.lower() in free_providers:
        return None
    # Strip TLD(s): acme.co.uk → acme
    name = domain.split(".")[0]
    return name.capitalize() if name else None


def _header(headers: list[dict], name: str) -> Optional[str]:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        creds = None
        if Path(self._token_file).exists():
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "w") as fh:
                fh.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        logger.info("Gmail authenticated.")

    # ------------------------------------------------------------------
    # History-based incremental fetch
    # ------------------------------------------------------------------

    def get_current_history_id(self) -> str:
        """Return the latest historyId from the inbox profile."""
        profile = self._service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def fetch_new_senders(self, since_history_id: str) -> tuple[list[SenderInfo], str]:
        """
        Return all new RECEIVED messages since *since_history_id*.
        Also returns the latest historyId to persist for the next run.
        """
        senders: list[SenderInfo] = []
        latest_history_id = since_history_id

        try:
            response = (
                self._service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=since_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId expired; reset to current
                logger.warning("History ID expired. Resetting.")
                return [], self.get_current_history_id()
            raise

        if "historyId" in response:
            latest_history_id = response["historyId"]

        for record in response.get("history", []):
            for msg_added in record.get("messagesAdded", []):
                msg = msg_added["message"]
                # Only process INBOX messages (skip SENT, DRAFTS, etc.)
                labels = msg.get("labelIds", [])
                if "INBOX" not in labels:
                    continue
                info = self._extract_sender(msg["id"])
                if info:
                    senders.append(info)

        return senders, latest_history_id

    # ------------------------------------------------------------------
    # Single message extraction
    # ------------------------------------------------------------------

    def _extract_sender(self, message_id: str) -> Optional[SenderInfo]:
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError as exc:
            logger.error("Failed to fetch message %s: %s", message_id, exc)
            return None

        headers = msg.get("payload", {}).get("headers", [])
        from_raw = _header(headers, "From") or ""
        subject = _header(headers, "Subject") or "(no subject)"
        date_raw = _header(headers, "Date") or ""

        display_name, email_addr = parseaddr(from_raw)
        email_addr = email_addr.strip().lower()

        if not email_addr or "@" not in email_addr:
            logger.debug("Skipping message %s — no valid sender email.", message_id)
            return None

        domain = email_addr.split("@")[1]
        first, last = _parse_name(display_name)
        company = _company_from_domain(domain)

        try:
            received_at = parsedate_to_datetime(date_raw).isoformat()
        except Exception:
            received_at = None

        return SenderInfo(
            email=email_addr,
            first_name=first,
            last_name=last,
            company=company,
            domain=domain,
            message_id=message_id,
            subject=subject,
            received_at=received_at,
        )
