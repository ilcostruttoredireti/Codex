import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional

logger = logging.getLogger(__name__)


def _google_imports():
    """Lazy import to avoid hard failures in environments without native crypto."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    return Request, Credentials, InstalledAppFlow, build, HttpError


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str, scopes: list[str]):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.scopes = scopes
        self._service = None

    def authenticate(self) -> None:
        Request, Credentials, InstalledAppFlow, build, HttpError = _google_imports()
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, self.scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, self.scopes
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authentication successful")

    def get_inbox_messages(
        self, since: Optional[datetime] = None
    ) -> Generator[dict, None, None]:
        """Yield raw message dicts from the inbox since the given datetime."""
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(days=1)

        # Gmail uses Unix epoch seconds in the after: filter
        after_epoch = int(since.timestamp())
        query = f"in:inbox after:{after_epoch}"

        page_token = None
        while True:
            try:
                _, _, _, _, HttpError = _google_imports()
                kwargs = {
                    "userId": "me",
                    "q": query,
                    "maxResults": 100,
                    "labelIds": ["INBOX"],
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                response = self._service.users().messages().list(**kwargs).execute()
                messages = response.get("messages", [])

                for msg_stub in messages:
                    full_msg = self._get_message(msg_stub["id"])
                    if full_msg:
                        yield full_msg

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

            except HttpError as e:
                logger.error("Gmail API error: %s", e)
                break

    def _get_message(self, message_id: str) -> Optional[dict]:
        _, _, _, _, HttpError = _google_imports()
        try:
            return (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError as e:
            logger.warning("Could not fetch message %s: %s", message_id, e)
            return None

    def extract_headers(self, message: dict) -> dict:
        """Return a flat dict of the requested metadata headers."""
        headers = {}
        for header in message.get("payload", {}).get("headers", []):
            headers[header["name"].lower()] = header["value"]
        return headers
