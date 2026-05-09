import json
import logging
import os
from typing import Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
logger = logging.getLogger(__name__)


class GmailClient:
    def __init__(
        self,
        credentials_file: str = 'credentials.json',
        token_file: str = 'token.json',
        state_file: str = '.gmail_state.json',
    ):
        self.token_file = token_file
        self.state_file = state_file
        self.service = self._authenticate(credentials_file)

    def _authenticate(self, credentials_file: str):
        creds: Optional[Credentials] = None

        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, 'w') as fh:
                fh.write(creds.to_json())

        return build('gmail', 'v1', credentials=creds)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _load_state(self) -> Dict:
        if os.path.exists(self.state_file):
            with open(self.state_file) as fh:
                return json.load(fh)
        return {}

    def _save_state(self, state: Dict) -> None:
        with open(self.state_file, 'w') as fh:
            json.dump(state, fh)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_new_messages(self) -> List[Dict]:
        """Return new INBOX messages since last call using the History API.

        On the very first call, returns the 50 most recent INBOX messages and
        records the current historyId so subsequent calls only return deltas.
        """
        state = self._load_state()

        if not state.get('history_id'):
            return self._bootstrap(state)

        return self._incremental(state)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _bootstrap(self, state: Dict) -> List[Dict]:
        """First-run: fetch recent messages and seed the history cursor."""
        messages: List[Dict] = []

        try:
            result = self.service.users().messages().list(
                userId='me',
                labelIds=['INBOX'],
                maxResults=50,
            ).execute()

            for stub in result.get('messages', []):
                detail = self._fetch_headers(stub['id'])
                if detail:
                    messages.append(detail)

            profile = self.service.users().getProfile(userId='me').execute()
            state['history_id'] = profile['historyId']
            self._save_state(state)

        except HttpError as exc:
            logger.error('Gmail bootstrap error: %s', exc)

        return messages

    def _incremental(self, state: Dict) -> List[Dict]:
        """Fetch only messages added to INBOX since the stored historyId."""
        messages: List[Dict] = []
        history_id = state['history_id']

        try:
            resp = self.service.users().history().list(
                userId='me',
                startHistoryId=history_id,
                historyTypes=['messageAdded'],
                labelId='INBOX',
            ).execute()

            new_history_id = resp.get('historyId', history_id)

            message_ids: set[str] = set()
            for item in resp.get('history', []):
                for added in item.get('messagesAdded', []):
                    msg = added.get('message', {})
                    if 'INBOX' in msg.get('labelIds', []):
                        message_ids.add(msg['id'])

            for msg_id in message_ids:
                detail = self._fetch_headers(msg_id)
                if detail:
                    messages.append(detail)

            state['history_id'] = new_history_id
            self._save_state(state)

        except HttpError as exc:
            logger.error('Gmail history error: %s', exc)

        return messages

    def _fetch_headers(self, message_id: str) -> Optional[Dict]:
        try:
            msg = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date'],
            ).execute()

            raw_headers = msg.get('payload', {}).get('headers', [])
            headers = {h['name']: h['value'] for h in raw_headers}

            return {
                'id': msg['id'],
                'from': headers.get('From', ''),
                'subject': headers.get('Subject', ''),
                'date': headers.get('Date', ''),
            }
        except HttpError as exc:
            logger.error('Gmail fetch message %s error: %s', message_id, exc)
            return None
