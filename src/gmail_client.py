import logging
import os
from typing import Iterator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_META_HEADERS = ["From", "Subject", "Date"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _build_service(self):
        if self._service:
            return self._service

        creds: Optional[Credentials] = None

        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self._credentials_file):
                    raise FileNotFoundError(
                        f"Gmail credentials not found at '{self._credentials_file}'. "
                        "Run setup_gmail_auth.py first."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, _SCOPES
                )
                creds = flow.run_local_server(port=0)

            _dir = os.path.dirname(self._token_file)
            if _dir:
                os.makedirs(_dir, exist_ok=True)
            with open(self._token_file, "w") as fh:
                fh.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_inbox_messages(
        self, since_timestamp: Optional[int] = None
    ) -> Iterator[dict]:
        service = self._build_service()

        query = "in:inbox -from:me"
        if since_timestamp:
            query += f" after:{since_timestamp}"

        page_token: Optional[str] = None

        try:
            while True:
                kwargs: dict = {"userId": "me", "q": query}
                if page_token:
                    kwargs["pageToken"] = page_token

                results = service.users().messages().list(**kwargs).execute()
                messages = results.get("messages", [])

                for ref in messages:
                    msg = (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=ref["id"],
                            format="metadata",
                            metadataHeaders=_META_HEADERS,
                        )
                        .execute()
                    )
                    yield msg

                page_token = results.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as exc:
            logger.error("Gmail API error: %s", exc)
            raise
