import base64
import logging
import os
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_SCOPES, GMAIL_PROCESSED_LABEL

logger = logging.getLogger("gmail_hubspot_sync.gmail")


class GmailMonitor:
    def __init__(self):
        self._service = None
        self._processed_label_id: str | None = None
        self._last_history_id: str | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _authenticate(self) -> None:
        creds: Credentials | None = None

        if os.path.exists(GMAIL_TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(GMAIL_TOKEN_FILE, "w") as token_file:
                token_file.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authentication successful.")

    def _ensure_service(self) -> None:
        if self._service is None:
            self._authenticate()

    # ------------------------------------------------------------------
    # Label management
    # ------------------------------------------------------------------

    def _get_or_create_label(self, label_name: str) -> str:
        """Return label ID, creating the label if it doesn't exist."""
        response = self._service.users().labels().list(userId="me").execute()
        for label in response.get("labels", []):
            if label["name"].lower() == label_name.lower():
                return label["id"]

        created = (
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
        logger.info("Created Gmail label '%s' (id=%s).", label_name, created["id"])
        return created["id"]

    def _ensure_processed_label(self) -> None:
        if self._processed_label_id is None:
            self._processed_label_id = self._get_or_create_label(GMAIL_PROCESSED_LABEL)

    # ------------------------------------------------------------------
    # History-based polling (efficient — avoids re-reading old messages)
    # ------------------------------------------------------------------

    def _get_start_history_id(self) -> str:
        """Fetch current historyId from the user's mailbox."""
        profile = self._service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def _fetch_new_message_ids(self) -> list[str]:
        """
        Use the Gmail History API to fetch only message IDs added since the
        last recorded historyId.  Falls back to the most recent 10 messages
        on first run.
        """
        if self._last_history_id is None:
            self._last_history_id = self._get_start_history_id()
            logger.info(
                "Starting from historyId=%s (no new messages on first run).",
                self._last_history_id,
            )
            return []

        new_ids: list[str] = []
        page_token = None

        try:
            while True:
                kwargs: dict = {
                    "userId": "me",
                    "startHistoryId": self._last_history_id,
                    "historyTypes": ["messageAdded"],
                    "labelId": "INBOX",
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                response = self._service.users().history().list(**kwargs).execute()

                for record in response.get("history", []):
                    for msg_added in record.get("messagesAdded", []):
                        msg = msg_added.get("message", {})
                        labels = msg.get("labelIds", [])
                        # Only INBOX messages, skip SENT / DRAFT
                        if "INBOX" in labels:
                            new_ids.append(msg["id"])

                new_history_id = response.get("historyId")
                if new_history_id:
                    self._last_history_id = new_history_id

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId expired — reset
                logger.warning("historyId expired, resetting.")
                self._last_history_id = self._get_start_history_id()
            else:
                raise

        return list(dict.fromkeys(new_ids))  # deduplicate, preserve order

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    def _get_sender(self, message_id: str) -> str | None:
        """Return the raw From header value for a message."""
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From"])
                .execute()
            )
            for header in msg.get("payload", {}).get("headers", []):
                if header["name"].lower() == "from":
                    return header["value"]
        except HttpError as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
        return None

    def mark_as_processed(self, message_id: str) -> None:
        """Apply the processed label to the Gmail message."""
        self._ensure_service()
        self._ensure_processed_label()
        try:
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [self._processed_label_id]},
            ).execute()
        except HttpError as exc:
            logger.warning("Could not label message %s: %s", message_id, exc)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def poll_new_senders(self) -> Generator[tuple[str, str], None, None]:
        """
        Yield (message_id, raw_from_header) for every new INBOX message
        since the last poll.
        """
        self._ensure_service()
        self._ensure_processed_label()

        new_ids = self._fetch_new_message_ids()
        logger.debug("Found %d new inbox message(s).", len(new_ids))

        for msg_id in new_ids:
            sender = self._get_sender(msg_id)
            if sender:
                yield msg_id, sender
