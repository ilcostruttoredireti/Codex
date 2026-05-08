"""Gmail API client for polling incoming emails."""
import email.utils
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Label applied to messages the sync has already processed
_PROCESSED_LABEL = "GmailHubSpotSynced"


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    full_name: str
    domain: str
    message_id: str
    subject: str


def _parse_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last); handle single-word names."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if parts:
        return parts[0], ""
    return "", ""


def _domain_from_email(addr: str) -> str:
    return addr.split("@")[-1].lower() if "@" in addr else ""


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None
        self._processed_label_id: str | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_credentials(self) -> Credentials:
        creds: Credentials | None = None
        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            Path(self._token_file).write_text(creds.to_json())

        return creds

    def connect(self) -> None:
        creds = self._get_credentials()
        self._service = build("gmail", "v1", credentials=creds)
        self._processed_label_id = self._ensure_label(_PROCESSED_LABEL)
        logger.info("Connected to Gmail API")

    # ------------------------------------------------------------------
    # Label helpers
    # ------------------------------------------------------------------

    def _ensure_label(self, name: str) -> str:
        """Return label ID, creating it if it doesn't exist."""
        labels = self._service.users().labels().list(userId="me").execute()
        for lbl in labels.get("labels", []):
            if lbl["name"] == name:
                return lbl["id"]
        created = (
            self._service.users()
            .labels()
            .create(userId="me", body={"name": name, "labelListVisibility": "labelHide"})
            .execute()
        )
        logger.info("Created Gmail label: %s (%s)", name, created["id"])
        return created["id"]

    def _mark_processed(self, message_id: str) -> None:
        self._service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [self._processed_label_id]},
        ).execute()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def iter_unprocessed_senders(self) -> Generator[SenderInfo, None, None]:
        """Yield SenderInfo for every incoming email not yet synced."""
        try:
            query = f"in:inbox -label:{_PROCESSED_LABEL}"
            response = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=100)
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail list error: %s", exc)
            return

        messages = response.get("messages", [])
        if not messages:
            return

        logger.info("Found %d unprocessed message(s)", len(messages))

        for msg_ref in messages:
            msg_id = msg_ref["id"]
            try:
                msg = (
                    self._service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="metadata",
                         metadataHeaders=["From", "Subject"])
                    .execute()
                )
            except HttpError as exc:
                logger.error("Failed to fetch message %s: %s", msg_id, exc)
                continue

            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            from_raw = headers.get("from", "")
            subject = headers.get("subject", "")

            if not from_raw:
                self._mark_processed(msg_id)
                continue

            parsed = email.utils.parseaddr(from_raw)
            display_name, addr = parsed
            addr = addr.lower().strip()

            if not addr or "@" not in addr:
                self._mark_processed(msg_id)
                continue

            first, last = _parse_name(display_name)
            sender = SenderInfo(
                email=addr,
                first_name=first,
                last_name=last,
                full_name=display_name.strip(),
                domain=_domain_from_email(addr),
                message_id=msg_id,
                subject=subject,
            )
            yield sender

            # Mark after yielding so a crash mid-processing doesn't skip re-try
            self._mark_processed(msg_id)
