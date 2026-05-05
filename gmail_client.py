import os
import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


class HistoryExpiredError(Exception):
    """Raised when Gmail reports the history ID has expired."""


class GmailClient:
    def __init__(self, credentials_file: str = 'credentials.json', token_file: str = 'token.json'):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None

    def authenticate(self) -> 'GmailClient':
        """Run OAuth2 flow (opens browser on first run, uses cached token after)."""
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("Gmail token refreshed")
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
                logger.info("Gmail OAuth2 flow completed")
            with open(self.token_file, 'w') as fh:
                fh.write(creds.to_json())

        self.service = build('gmail', 'v1', credentials=creds)
        return self

    # ------------------------------------------------------------------
    # History-based incremental polling
    # ------------------------------------------------------------------

    def get_profile(self) -> dict:
        return self.service.users().getProfile(userId='me').execute()

    def get_initial_history_id(self) -> str:
        """Return the current historyId to use as a starting baseline."""
        return self.get_profile()['historyId']

    def list_new_messages(self, start_history_id: str) -> list[dict]:
        """
        Return stubs for messages newly added to INBOX since start_history_id.
        Each stub has at least {'id': ..., 'threadId': ..., 'labelIds': [...]}.
        Raises HistoryExpiredError if the history ID is too old.
        """
        messages: list[dict] = []
        page_token = None

        while True:
            try:
                params: dict = {
                    'userId': 'me',
                    'startHistoryId': start_history_id,
                    'historyTypes': ['messageAdded'],
                    'labelId': 'INBOX',
                }
                if page_token:
                    params['pageToken'] = page_token

                result = self.service.users().history().list(**params).execute()

                for entry in result.get('history', []):
                    for added in entry.get('messagesAdded', []):
                        msg = added.get('message', {})
                        if 'INBOX' in msg.get('labelIds', []):
                            messages.append(msg)

                page_token = result.get('nextPageToken')
                if not page_token:
                    break

            except HttpError as exc:
                if exc.resp.status == 404:
                    raise HistoryExpiredError("History ID expired") from exc
                raise

        return messages

    def get_message(self, msg_id: str) -> dict:
        """Fetch full message metadata (From, Subject, Date headers)."""
        return self.service.users().messages().get(
            userId='me',
            id=msg_id,
            format='metadata',
            metadataHeaders=['From', 'Subject', 'Date'],
        ).execute()

    def get_current_history_id(self) -> str:
        """Refresh and return the latest historyId after a polling cycle."""
        return self.get_profile()['historyId']
