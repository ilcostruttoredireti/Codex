"""Gmail API client — authenticates, polls for new messages, extracts sender info."""

import base64
import email as email_lib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import config

logger = logging.getLogger(__name__)


@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    domain: str = ""
    company: str = ""
    message_id: str = ""
    subject: str = ""

    def __post_init__(self) -> None:
        if self.email and not self.domain:
            self.domain = self.email.split("@")[-1].lower()
        if self.domain and not self.company:
            self.company = _domain_to_company(self.domain)


def _domain_to_company(domain: str) -> str:
    """Best-effort: strip TLD + known sub-domains to get a company name."""
    parts = domain.split(".")
    # Remove common subdomains
    if len(parts) > 2 and parts[0] in ("mail", "smtp", "mx", "email", "m"):
        parts = parts[1:]
    # Drop TLD(s) — keep root label
    name = parts[0] if parts else domain
    return name.capitalize()


def _parse_name(raw_name: str) -> tuple[str, str]:
    """Split a display name into (first_name, last_name)."""
    raw_name = raw_name.strip().strip('"').strip("'")
    parts = raw_name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _extract_sender(from_header: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a From header."""
    # Handles: "Name <email>" or just "email"
    match = re.match(r"^(.*?)\s*<([^>]+)>$", from_header.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip().lower()
    # Plain address only
    addr = from_header.strip().lower()
    return "", addr


class GmailClient:
    def __init__(self) -> None:
        self._service = None
        self._processed_label_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Perform OAuth2 flow (interactive on first run, cached afterward)."""
        creds: Optional[Credentials] = None

        if os.path.exists(config.GMAIL_TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(
                config.GMAIL_TOKEN_FILE, config.GMAIL_SCOPES
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(config.GMAIL_TOKEN_FILE, "w") as token_file:
                token_file.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authentication successful.")

    # ------------------------------------------------------------------
    # Label helpers
    # ------------------------------------------------------------------

    def _get_or_create_label(self, name: str) -> str:
        """Return the Gmail label ID for *name*, creating it if missing."""
        labels = self._service.users().labels().list(userId="me").execute()
        for lbl in labels.get("labels", []):
            if lbl["name"].lower() == name.lower():
                return lbl["id"]

        new_label = (
            self._service.users()
            .labels()
            .create(userId="me", body={"name": name})
            .execute()
        )
        logger.info("Created Gmail label '%s' (id=%s).", name, new_label["id"])
        return new_label["id"]

    def _ensure_processed_label(self) -> str:
        if self._processed_label_id is None:
            self._processed_label_id = self._get_or_create_label(
                config.GMAIL_PROCESSED_LABEL
            )
        return self._processed_label_id

    def mark_as_processed(self, message_id: str) -> None:
        """Apply the processed label so we skip this message next poll."""
        label_id = self._ensure_processed_label()
        self._service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    # ------------------------------------------------------------------
    # Fetching new messages
    # ------------------------------------------------------------------

    def fetch_new_messages(self) -> list[SenderInfo]:
        """
        Return SenderInfo for every unprocessed inbox message,
        up to MAX_EMAILS_PER_POLL.
        """
        label_id = self._ensure_processed_label()

        # Query: INBOX messages that do NOT yet have our processed label
        query = f"in:inbox -label:{config.GMAIL_PROCESSED_LABEL}"
        try:
            result = (
                self._service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=config.MAX_EMAILS_PER_POLL,
                )
                .execute()
            )
        except HttpError as exc:
            logger.error("Error listing Gmail messages: %s", exc)
            return []

        messages = result.get("messages", [])
        if not messages:
            logger.debug("No new messages found.")
            return []

        senders: list[SenderInfo] = []
        for msg_stub in messages:
            info = self._parse_message(msg_stub["id"])
            if info:
                senders.append(info)

        return senders

    def _parse_message(self, message_id: str) -> Optional[SenderInfo]:
        """Fetch full message headers and extract sender data."""
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

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        from_header = headers.get("from", "")
        subject = headers.get("subject", "")

        if not from_header:
            return None

        display_name, email_addr = _extract_sender(from_header)

        if not email_addr or "@" not in email_addr:
            return None

        local_part = email_addr.split("@")[0].lower()
        domain = email_addr.split("@")[1].lower()

        # Skip automated / no-reply senders
        if (
            local_part in config.IGNORED_LOCAL_PARTS
            or domain in config.IGNORED_DOMAINS
            or any(kw in local_part for kw in ("noreply", "no-reply", "donotreply"))
        ):
            logger.debug("Skipping automated sender: %s", email_addr)
            return None

        first_name, last_name = _parse_name(display_name)

        return SenderInfo(
            email=email_addr,
            first_name=first_name,
            last_name=last_name,
            full_name=display_name,
            domain=domain,
            message_id=message_id,
            subject=subject,
        )
