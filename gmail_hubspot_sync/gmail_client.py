"""Gmail API wrapper — authentication, message listing, history polling."""

import base64
import logging
import os
from typing import Iterator, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


def _build_service(credentials_file: str, token_file: str, scopes: list):
    creds: Optional[Credentials] = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, scopes)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str, scopes: list):
        self._service = _build_service(credentials_file, token_file, scopes)

    # ------------------------------------------------------------------
    # Label management
    # ------------------------------------------------------------------

    def get_or_create_label(self, name: str) -> str:
        """Return the label ID for *name*, creating the label if it doesn't exist."""
        labels = self._service.users().labels().list(userId="me").execute()
        for lbl in labels.get("labels", []):
            if lbl["name"].lower() == name.lower():
                return lbl["id"]
        created = (
            self._service.users()
            .labels()
            .create(userId="me", body={"name": name, "labelListVisibility": "labelShow",
                                       "messageListVisibility": "show"})
            .execute()
        )
        logger.info("Created Gmail label '%s' (id=%s)", name, created["id"])
        return created["id"]

    def apply_label(self, message_id: str, label_id: str) -> None:
        try:
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except HttpError as exc:
            logger.warning("Could not apply label to %s: %s", message_id, exc)

    # ------------------------------------------------------------------
    # Initial full-inbox scan
    # ------------------------------------------------------------------

    def get_current_history_id(self) -> str:
        """Return the mailbox's current historyId (used to anchor incremental polls)."""
        profile = self._service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def list_inbox_messages(self, max_results: int = 100) -> Iterator[dict]:
        """Yield basic message stubs from INBOX (for the first run)."""
        page_token = None
        fetched = 0
        while fetched < max_results:
            batch = min(100, max_results - fetched)
            resp = (
                self._service.users()
                .messages()
                .list(
                    userId="me",
                    labelIds=["INBOX"],
                    maxResults=batch,
                    pageToken=page_token,
                )
                .execute()
            )
            for msg in resp.get("messages", []):
                yield msg
                fetched += 1
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    # ------------------------------------------------------------------
    # Incremental sync via History API
    # ------------------------------------------------------------------

    def get_new_message_ids(self, start_history_id: str) -> Tuple[list[str], str]:
        """Return (new_message_ids, latest_history_id) since *start_history_id*."""
        new_ids: list[str] = []
        page_token = None
        latest_id = start_history_id

        try:
            while True:
                resp = (
                    self._service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        labelId="INBOX",
                        pageToken=page_token,
                    )
                    .execute()
                )
                latest_id = str(resp.get("historyId", latest_id))
                for record in resp.get("history", []):
                    for added in record.get("messagesAdded", []):
                        msg = added.get("message", {})
                        labels = msg.get("labelIds", [])
                        # Only process messages that arrived in INBOX
                        if "INBOX" in labels:
                            new_ids.append(msg["id"])
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId too old — caller should do a full rescan
                logger.warning("historyId expired, full rescan needed: %s", exc)
                return [], latest_id
            raise

        return new_ids, latest_id

    # ------------------------------------------------------------------
    # Message detail
    # ------------------------------------------------------------------

    def get_message_sender(self, message_id: str) -> Optional[str]:
        """Return the raw 'From' header value for *message_id*, or None."""
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
            for header in msg.get("payload", {}).get("headers", []):
                if header["name"].lower() == "from":
                    return header["value"]
        except HttpError as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
        return None
