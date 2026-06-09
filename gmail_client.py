import os
import logging
from typing import Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._build_service()

    def _build_service(self):
        creds: Optional[Credentials] = None

        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.debug("Gmail token refreshed")
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, _SCOPES
                )
                creds = flow.run_local_server(port=0)
                logger.info("Gmail OAuth2 flow completed")

            with open(self.token_file, 'w') as fh:
                fh.write(creds.to_json())

        service = build('gmail', 'v1', credentials=creds)
        logger.info("Gmail client ready")
        return service

    def list_inbox_messages(self, max_results: int = 50) -> List[Dict]:
        """Return a list of {id, threadId} dicts from INBOX."""
        try:
            resp = self.service.users().messages().list(
                userId='me',
                labelIds=['INBOX'],
                maxResults=max_results,
            ).execute()
            return resp.get('messages', [])
        except HttpError as exc:
            logger.error(f"Gmail list error: {exc}")
            return []

    def get_message_headers(self, message_id: str) -> Optional[Dict]:
        """Fetch a message with only From / Subject / Date headers."""
        try:
            return self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date'],
            ).execute()
        except HttpError as exc:
            logger.error(f"Gmail get message {message_id} error: {exc}")
            return None

    @staticmethod
    def get_header(message: Dict, name: str) -> Optional[str]:
        """Extract a named header value from a message dict."""
        headers: List[Dict] = message.get('payload', {}).get('headers', [])
        name_lower = name.lower()
        for h in headers:
            if h.get('name', '').lower() == name_lower:
                return h.get('value')
        return None
