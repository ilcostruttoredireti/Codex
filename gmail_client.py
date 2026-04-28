"""Thin wrapper around the Gmail API (read-only OAuth2 scope)."""

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(
        self,
        credentials_file: str = "",
        token_file: str = "",
    ):
        self._creds_file = Path(
            credentials_file or os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
        )
        self._token_file = Path(
            token_file or os.getenv("GMAIL_TOKEN_FILE", "token.json")
        )
        self.service = self._authenticate()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self):
        creds: Credentials | None = None

        if self._token_file.exists():
            creds = Credentials.from_authorized_user_file(str(self._token_file), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self._creds_file.exists():
                    raise FileNotFoundError(
                        f"Gmail credentials file not found: {self._creds_file}\n"
                        "Download it from Google Cloud Console → APIs & Services → Credentials\n"
                        "and save it as 'credentials.json' (or set GMAIL_CREDENTIALS_FILE)."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._creds_file), SCOPES
                )
                creds = flow.run_local_server(port=0)

            self._token_file.write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # ------------------------------------------------------------------
    # Message fetching
    # ------------------------------------------------------------------

    def get_messages_after(
        self,
        since_epoch: int,
        max_results: int = 500,
    ) -> Generator[dict, None, None]:
        """
        Yield Gmail message metadata (From, Subject, Date headers) for every
        inbox message received after *since_epoch* (Unix seconds).

        On first run (since_epoch == 0) only the last 24 h are fetched to
        avoid flooding HubSpot with historical data.
        """
        if since_epoch <= 0:
            since_epoch = int(time.time()) - 86_400

        since_ms = since_epoch * 1000
        since_date = datetime.utcfromtimestamp(since_epoch).strftime("%Y/%m/%d")
        query = f"after:{since_date} in:inbox"

        page_token = None
        try:
            while True:
                kwargs = dict(userId="me", q=query, maxResults=min(max_results, 500))
                if page_token:
                    kwargs["pageToken"] = page_token

                result = self.service.users().messages().list(**kwargs).execute()
                refs = result.get("messages", [])

                for ref in refs:
                    msg = (
                        self.service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=ref["id"],
                            format="metadata",
                            metadataHeaders=["From", "Subject", "Date"],
                        )
                        .execute()
                    )
                    # Secondary filter by exact internalDate
                    if int(msg.get("internalDate", 0)) >= since_ms:
                        yield msg

                page_token = result.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as exc:
            raise RuntimeError(f"Gmail API error: {exc}") from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_header(message: dict, name: str) -> str:
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == name.lower():
                return h["value"]
        return ""
