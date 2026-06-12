"""Gmail API client — auth, message listing, label management."""
import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

log = logging.getLogger(__name__)


class GmailReader:
    def __init__(self):
        self.service = self._authenticate()
        self._label_cache: dict[str, str] = {}

    def _authenticate(self):
        creds = None
        if os.path.exists(config.GMAIL_TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(config.GMAIL_TOKEN_FILE, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    config.GMAIL_CREDENTIALS_FILE, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(config.GMAIL_TOKEN_FILE, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ── message fetching ──────────────────────────────────────────────────────

    def get_unprocessed_messages(self, max_results: int = 200) -> list[dict]:
        """Return inbox messages not yet labelled as processed."""
        label_slug = config.GMAIL_PROCESSED_LABEL.lower().replace(" ", "-")
        query = f"in:inbox -from:me -label:{label_slug}"

        messages: list[dict] = []
        page_token = None

        while True:
            try:
                kwargs: dict = {"userId": "me", "q": query, "maxResults": min(500, max_results)}
                if page_token:
                    kwargs["pageToken"] = page_token

                result = self.service.users().messages().list(**kwargs).execute()
                messages.extend(result.get("messages", []))
                page_token = result.get("nextPageToken")

                if not page_token or len(messages) >= max_results:
                    break
            except HttpError as exc:
                log.error("Gmail list error: %s", exc)
                break

        return messages

    def get_from_header(self, message_id: str) -> str | None:
        """Fetch only the From: header for a message."""
        try:
            msg = self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            return headers.get("From")
        except HttpError as exc:
            log.error("Gmail get message %s error: %s", message_id, exc)
            return None

    # ── label management ──────────────────────────────────────────────────────

    def get_or_create_label(self, name: str) -> str | None:
        if name in self._label_cache:
            return self._label_cache[name]

        try:
            result = self.service.users().labels().list(userId="me").execute()
            for lbl in result.get("labels", []):
                if lbl["name"].lower() == name.lower():
                    self._label_cache[name] = lbl["id"]
                    return lbl["id"]

            new_lbl = self.service.users().labels().create(
                userId="me",
                body={"name": name, "labelListVisibility": "labelShow"},
            ).execute()
            log.info("Created Gmail label '%s'", name)
            self._label_cache[name] = new_lbl["id"]
            return new_lbl["id"]
        except HttpError as exc:
            log.error("Gmail label error: %s", exc)
            return None

    def mark_processed(self, message_id: str, label_id: str):
        try:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except HttpError as exc:
            log.error("Gmail mark processed error: %s", exc)
