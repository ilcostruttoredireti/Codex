"""
gmail_client.py – Gmail API wrapper (OAuth2, message fetching, label management)
"""
from __future__ import annotations

import base64
import email as email_lib
import re
from pathlib import Path
from typing import Generator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import config
from logger import get_logger
from models import EmailMessage, SenderInfo

logger = get_logger()

# Regex to parse "Name <email>" or plain "email" from a From: header
_FROM_RE = re.compile(r'^(?:"?(?P<name>[^"<>]+?)"?\s+)?<(?P<addr>[^>]+)>$|^(?P<bare>[^\s<>]+@[^\s<>]+)$')


def _parse_from_header(header: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a From: header value."""
    header = header.strip()
    m = _FROM_RE.match(header)
    if m:
        name = (m.group("name") or "").strip().strip('"')
        addr = (m.group("addr") or m.group("bare") or "").strip()
        return name, addr
    # Fallback: treat the whole string as an address
    return "", header


def _split_name(full_name: str) -> tuple[str, str]:
    """Best-effort split of a display name into (first, last)."""
    parts = full_name.strip().split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _company_from_domain(domain: str) -> str:
    """Derive a company name from a corporate email domain."""
    if domain in config.PERSONAL_DOMAINS:
        return ""
    # Strip common TLDs and return capitalised label
    # e.g. "acme.com" → "Acme"  |  "mail.acme.co.uk" → "Acme"
    parts = domain.split(".")
    # Walk from the right: skip known TLD parts
    known_tld_parts = {"com", "net", "org", "it", "co", "uk", "de", "fr", "io", "ai"}
    significant = [p for p in parts if p not in known_tld_parts]
    if significant:
        return significant[-1].capitalize()
    return parts[0].capitalize()


class GmailClient:
    def __init__(self) -> None:
        self._service = None
        self._processed_label_id: Optional[str] = None

    # ── Authentication ────────────────────────────────────────

    def authenticate(self) -> None:
        """Run OAuth2 flow (opens browser on first run) and build the API service."""
        creds: Optional[Credentials] = None
        token_path = Path(config.GMAIL_TOKEN_PATH)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), config.GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing Gmail access token…")
                creds.refresh(Request())
            else:
                logger.info("Starting Gmail OAuth2 flow – a browser window will open…")
                flow = InstalledAppFlow.from_client_secrets_file(
                    config.GMAIL_CREDENTIALS_PATH, config.GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(token_path, "w") as f:
                f.write(creds.to_json())
            logger.info("Gmail token saved to %s", token_path)

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail API authenticated ✓")
        self._processed_label_id = self._ensure_label(config.GMAIL_PROCESSED_LABEL)

    # ── Label helpers ─────────────────────────────────────────

    def _ensure_label(self, label_name: str) -> str:
        """Return the label ID, creating the label if it does not exist."""
        labels_resp = self._service.users().labels().list(userId="me").execute()
        for lbl in labels_resp.get("labels", []):
            if lbl["name"].lower() == label_name.lower():
                logger.debug("Found existing label '%s' (id=%s)", label_name, lbl["id"])
                return lbl["id"]

        new_label = (
            self._service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": label_name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        logger.info("Created Gmail label '%s' (id=%s)", label_name, new_label["id"])
        return new_label["id"]

    def mark_as_processed(self, message_id: str) -> None:
        """Apply the processed label to a message."""
        try:
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [self._processed_label_id]},
            ).execute()
        except HttpError as exc:
            logger.warning("Could not label message %s: %s", message_id, exc)

    # ── Message fetching ──────────────────────────────────────

    def _build_query(self) -> str:
        """Build the Gmail search query string."""
        # Exclude messages already labelled as processed
        # Also exclude messages we sent (SENT label)
        parts = [
            "in:inbox",
            f"-label:{config.GMAIL_PROCESSED_LABEL}",
        ]
        if config.SYNC_SINCE_DATE:
            # Gmail supports after: with YYYY/MM/DD
            date_part = config.SYNC_SINCE_DATE[:10].replace("-", "/")
            parts.append(f"after:{date_part}")
        return " ".join(parts)

    def fetch_new_messages(self) -> Generator[EmailMessage, None, None]:
        """Yield EmailMessage objects for every unprocessed inbox message."""
        query = self._build_query()
        logger.debug("Gmail query: %s", query)

        next_page_token: Optional[str] = None
        while True:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": 100}
            if next_page_token:
                kwargs["pageToken"] = next_page_token

            try:
                resp = self._service.users().messages().list(**kwargs).execute()
            except HttpError as exc:
                logger.error("Gmail list error: %s", exc)
                return

            messages = resp.get("messages", [])
            logger.debug("Fetched %d message IDs from Gmail", len(messages))

            for msg_ref in messages:
                msg = self._fetch_message_detail(msg_ref["id"])
                if msg:
                    yield msg

            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break

    def _fetch_message_detail(self, message_id: str) -> Optional[EmailMessage]:
        """Fetch headers + snippet for a single message."""
        try:
            raw = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError as exc:
            logger.error("Error fetching message %s: %s", message_id, exc)
            return None

        headers: dict[str, str] = {
            h["name"]: h["value"]
            for h in raw.get("payload", {}).get("headers", [])
        }

        from_header = headers.get("From", "")
        if not from_header:
            return None

        display_name, addr = _parse_from_header(from_header)

        if not addr or "@" not in addr:
            logger.debug("Skipping message %s – unparseable From: %s", message_id, from_header)
            return None

        first, last = _split_name(display_name)
        domain = addr.split("@", 1)[1].lower()
        company = _company_from_domain(domain)

        sender = SenderInfo(
            email=addr,
            first_name=first,
            last_name=last,
            full_name=display_name,
            company=company,
            domain=domain,
        )

        return EmailMessage(
            message_id=message_id,
            thread_id=raw.get("threadId", ""),
            subject=headers.get("Subject", "(no subject)"),
            sender=sender,
            received_at=headers.get("Date", ""),
            snippet=raw.get("snippet", ""),
        )
