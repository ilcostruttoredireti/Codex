"""
Gmail monitor: authenticates via OAuth2 and polls the inbox for new messages.
Uses a local state file to track the last processed historyId so restarts are
safe and no message is processed twice.
"""

import json
import logging
import os
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path("gmail_state.json")


class GmailMonitor:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.json"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None
        self._last_history_id: str | None = self._load_state()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        self.service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authenticated successfully.")

    # ------------------------------------------------------------------
    # State persistence (tracks historyId across restarts)
    # ------------------------------------------------------------------

    def _load_state(self) -> str | None:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                return data.get("last_history_id")
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def _save_state(self, history_id: str) -> None:
        STATE_FILE.write_text(json.dumps({"last_history_id": history_id}))
        self._last_history_id = history_id

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _get_current_history_id(self) -> str:
        """Return the historyId of the most recent message in the inbox."""
        result = self.service.users().messages().list(
            userId="me", maxResults=1, labelIds=["INBOX"]
        ).execute()
        messages = result.get("messages", [])
        if not messages:
            profile = self.service.users().getProfile(userId="me").execute()
            return str(profile["historyId"])
        msg = self.service.users().messages().get(
            userId="me", id=messages[0]["id"], format="minimal"
        ).execute()
        return str(msg["historyId"])

    def _get_new_messages(self) -> list[dict]:
        """
        Return full message dicts for every new INBOX message since the last
        stored historyId.  Seeds the historyId on the very first call.
        """
        if not self._last_history_id:
            current_id = self._get_current_history_id()
            self._save_state(current_id)
            logger.info("First run – seeded historyId %s; waiting for new mail.", current_id)
            return []

        new_messages: list[dict] = []
        try:
            history_resp = self.service.users().history().list(
                userId="me",
                startHistoryId=self._last_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()
        except HttpError as exc:
            # historyId expired (code 404) – reseed
            if exc.resp.status == 404:
                logger.warning("historyId expired – reseeding.")
                self._save_state(self._get_current_history_id())
                return []
            raise

        history = history_resp.get("history", [])
        seen_ids: set[str] = set()

        for record in history:
            for added in record.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                try:
                    full = self.service.users().messages().get(
                        userId="me", id=msg_id, format="full"
                    ).execute()
                    new_messages.append(full)
                except HttpError as e:
                    logger.warning("Could not fetch message %s: %s", msg_id, e)

        # Advance the stored historyId
        new_history_id = history_resp.get("historyId", self._last_history_id)
        self._save_state(new_history_id)

        return new_messages

    def poll(self, interval_seconds: int = 60):
        """
        Generator: yields new Gmail message dicts as they arrive.
        Blocks between polls for *interval_seconds*.
        """
        if self.service is None:
            raise RuntimeError("Call authenticate() before polling.")

        logger.info("Starting Gmail poll loop (interval=%ds).", interval_seconds)
        while True:
            try:
                messages = self._get_new_messages()
                for msg in messages:
                    yield msg
            except Exception as exc:
                logger.error("Error during Gmail poll: %s", exc, exc_info=True)

            time.sleep(interval_seconds)
