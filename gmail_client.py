import logging
from email.utils import parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str, user_id: str = 'me'):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.user_id = user_id
        self.service = self._authenticate()
        self._history_id: Optional[str] = None  # type: ignore[name-defined]

    def _authenticate(self):
        creds = None
        token_path = Path(self.token_file)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json())

        return build('gmail', 'v1', credentials=creds)

    # ------------------------------------------------------------------
    # History-based polling
    # ------------------------------------------------------------------

    def initialize_history(self) -> str:
        """Record the current historyId so future polls only return new mail."""
        profile = self.service.users().getProfile(userId=self.user_id).execute()
        self._history_id = profile['historyId']
        logger.info("History initialized at ID %s", self._history_id)
        return self._history_id

    def get_new_message_ids(self) -> list:
        """
        Return message IDs for emails that arrived in INBOX since the last call.
        Safe to call repeatedly; updates the internal history cursor on each call.
        """
        if not self._history_id:
            self.initialize_history()
            return []

        try:
            response = (
                self.service.users()
                .history()
                .list(
                    userId=self.user_id,
                    startHistoryId=self._history_id,
                    historyTypes=['messageAdded'],
                    labelId='INBOX',
                )
                .execute()
            )
        except HttpError as exc:
            # historyId too old (410 Gone) → reinitialise and skip this cycle
            if exc.resp.status == 410:
                logger.warning("historyId expired, reinitialising cursor")
                self.initialize_history()
                return []
            raise

        new_history_id = response.get('historyId', self._history_id)
        message_ids = []

        for record in response.get('history', []):
            for added in record.get('messagesAdded', []):
                msg = added.get('message', {})
                if 'INBOX' in msg.get('labelIds', []):
                    message_ids.append(msg['id'])

        self._history_id = new_history_id
        return message_ids

    # ------------------------------------------------------------------
    # Message metadata
    # ------------------------------------------------------------------

    def get_sender_info(self, message_id: str) -> dict:
        """Fetch From / Subject / Date headers from a single message."""
        msg = (
            self.service.users()
            .messages()
            .get(
                userId=self.user_id,
                id=message_id,
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date'],
            )
            .execute()
        )

        headers = {h['name']: h['value'] for h in msg['payload']['headers']}
        from_raw = headers.get('From', '')
        display_name, email_addr = parseaddr(from_raw)

        return {
            'message_id': message_id,
            'from_raw': from_raw,
            'name': display_name,
            'email': email_addr.lower().strip(),
            'subject': headers.get('Subject', ''),
            'date': headers.get('Date', ''),
        }
