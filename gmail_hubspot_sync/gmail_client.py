"""
Gmail client using OAuth2.

First run: opens browser for consent and saves token.json.
Subsequent runs: reuses token.json (auto-refreshes when expired).

Required env vars (or .env):
  GMAIL_CREDENTIALS_FILE  path to credentials.json from Google Cloud Console
  GMAIL_TOKEN_FILE        path where the OAuth token is persisted (default: token.json)
  GMAIL_STATE_FILE        path where the last historyId is stored  (default: gmail_state.json)
"""

import base64
import json
import os
from pathlib import Path
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(
        self,
        credentials_file: str,
        token_file: str = "token.json",
        state_file: str = "gmail_state.json",
    ):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._state_file = state_file
        self._service = self._build_service()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _build_service(self):
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

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # State persistence (historyId)
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        if Path(self._state_file).exists():
            with open(self._state_file) as fh:
                return json.load(fh)
        return {}

    def _save_state(self, state: dict) -> None:
        with open(self._state_file, "w") as fh:
            json.dump(state, fh)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_new_messages(self) -> Generator[dict, None, None]:
        """
        Yield new inbox messages since the last call.
        Each item: {"id": str, "from": str, "subject": str, "snippet": str}
        """
        state = self._load_state()
        history_id = state.get("history_id")

        if history_id:
            yield from self._messages_from_history(history_id)
        else:
            yield from self._messages_initial_scan()

    def _messages_initial_scan(self) -> Generator[dict, None, None]:
        """First-run: grab the 50 most recent inbox messages and record historyId."""
        result = (
            self._service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=50)
            .execute()
        )
        messages = result.get("messages", [])
        latest_history_id = None

        for msg_stub in messages:
            msg = self._fetch_message(msg_stub["id"])
            if msg:
                latest_history_id = latest_history_id or msg.get("historyId")
                yield self._parse_message(msg)

        if not latest_history_id:
            # No messages at all – get current profile historyId
            profile = self._service.users().getProfile(userId="me").execute()
            latest_history_id = profile.get("historyId")

        self._save_state({"history_id": latest_history_id})

    def _messages_from_history(self, start_history_id: str) -> Generator[dict, None, None]:
        """Incremental fetch using Gmail History API."""
        new_history_id = start_history_id
        page_token = None

        while True:
            kwargs = dict(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                result = self._service.users().history().list(**kwargs).execute()
            except Exception as exc:
                # historyId expired (>30 days) → reset
                if "404" in str(exc) or "invalidHistoryId" in str(exc):
                    self._save_state({})
                    return
                raise

            for record in result.get("history", []):
                new_history_id = record.get("id", new_history_id)
                for added in record.get("messagesAdded", []):
                    labels = added.get("message", {}).get("labelIds", [])
                    if "INBOX" not in labels:
                        continue
                    msg = self._fetch_message(added["message"]["id"])
                    if msg:
                        yield self._parse_message(msg)

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        self._save_state({"history_id": new_history_id})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_message(self, msg_id: str) -> dict | None:
        try:
            return (
                self._service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
        except Exception:
            return None

    @staticmethod
    def _parse_message(msg: dict) -> dict:
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        return {
            "id": msg["id"],
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "snippet": msg.get("snippet", ""),
            "history_id": msg.get("historyId"),
        }
