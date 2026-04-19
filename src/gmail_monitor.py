import json
import logging
import os
from pathlib import Path
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_STATE_FILE = ".gmail_state.json"
_MAX_RESULTS = 50
# Keep at most this many processed IDs in state to bound file size
_MAX_TRACKED_IDS = 20_000


class GmailMonitor:
    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
    ):
        self._token_file = token_file
        self._credentials_file = credentials_file
        self._service = self._authenticate()
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _authenticate(self):
        creds: Credentials | None = None

        if Path(self._token_file).exists():
            creds = Credentials.from_authorized_user_file(self._token_file, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("Gmail token refreshed")
            else:
                if not Path(self._credentials_file).exists():
                    raise FileNotFoundError(
                        f"Gmail OAuth credentials not found: {self._credentials_file}\n"
                        "Download them from Google Cloud Console → APIs & Services → Credentials."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, _SCOPES
                )
                creds = flow.run_local_server(port=0)
                logger.info("Gmail OAuth flow completed")

            with open(self._token_file, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # State persistence (tracks processed message IDs)
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        if Path(_STATE_FILE).exists():
            try:
                with open(_STATE_FILE) as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
        return {"processed_ids": []}

    def save_state(self) -> None:
        ids = self._state["processed_ids"]
        if len(ids) > _MAX_TRACKED_IDS:
            self._state["processed_ids"] = ids[-_MAX_TRACKED_IDS:]
        with open(_STATE_FILE, "w") as fh:
            json.dump(self._state, fh)

    # ------------------------------------------------------------------
    # Email fetching
    # ------------------------------------------------------------------

    def fetch_new_emails(self) -> list[dict]:
        """Return Gmail message objects (metadata) not yet processed."""
        processed = set(self._state["processed_ids"])
        new_messages: list[dict] = []

        try:
            result = (
                self._service.users()
                .messages()
                .list(userId="me", labelIds=["INBOX"], maxResults=_MAX_RESULTS)
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail list error: %s", exc)
            return []

        for ref in result.get("messages", []):
            msg_id = ref["id"]
            if msg_id in processed:
                continue

            try:
                msg = (
                    self._service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=msg_id,
                        format="metadata",
                        metadataHeaders=["From", "Subject", "Date"],
                    )
                    .execute()
                )
            except HttpError as exc:
                logger.warning("Could not fetch message %s: %s", msg_id, exc)
                continue

            self._state["processed_ids"].append(msg_id)
            new_messages.append(msg)

        logger.info("Fetched %d new message(s) from Gmail", len(new_messages))
        return new_messages
