import logging
import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None

    def authenticate(self):
        creds = None

        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"File credenziali Gmail non trovato: {self.credentials_file}\n"
                        "Scarica il file da Google Cloud Console (API & Services → Credentials)."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(self.token_file, 'w') as f:
                f.write(creds.to_json())

        self.service = build('gmail', 'v1', credentials=creds)
        logger.info("Autenticazione Gmail completata.")

    def get_profile_history_id(self) -> Optional[str]:
        try:
            profile = self.service.users().getProfile(userId='me').execute()
            return str(profile['historyId'])
        except HttpError as e:
            logger.error(f"Errore getProfile: {e}")
            return None

    def poll_inbox_messages(self, start_history_id: str) -> tuple:
        """Return (list_of_message_ids, new_history_id).

        Fetches all history records since start_history_id filtered to INBOX
        messageAdded events. The returned new_history_id should replace
        start_history_id in the next call.
        """
        message_ids = []
        new_history_id = start_history_id
        page_token = None

        try:
            while True:
                kwargs = {
                    'userId': 'me',
                    'startHistoryId': start_history_id,
                    'historyTypes': ['messageAdded'],
                    'labelId': 'INBOX',
                }
                if page_token:
                    kwargs['pageToken'] = page_token

                response = self.service.users().history().list(**kwargs).execute()

                new_history_id = response.get('historyId', new_history_id)

                for record in response.get('history', []):
                    for added in record.get('messagesAdded', []):
                        msg = added.get('message', {})
                        if 'INBOX' in msg.get('labelIds', []):
                            message_ids.append(msg['id'])

                page_token = response.get('nextPageToken')
                if not page_token:
                    break

        except HttpError as e:
            if e.resp.status == 404:
                logger.warning(
                    f"History ID {start_history_id} scaduto, reset al corrente."
                )
                new_history_id = self.get_profile_history_id() or start_history_id
            else:
                logger.error(f"Errore Gmail history: {e}")

        return message_ids, new_history_id

    def get_message_from_header(self, message_id: str) -> tuple:
        """Return (from_header, subject) for a message, or (None, None) on error."""
        try:
            msg = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='metadata',
                metadataHeaders=['From', 'Subject'],
            ).execute()

            headers = {
                h['name'].lower(): h['value']
                for h in msg.get('payload', {}).get('headers', [])
            }
            return headers.get('from'), headers.get('subject', '(nessun oggetto)')

        except HttpError as e:
            logger.error(f"Errore recupero messaggio {message_id}: {e}")
            return None, None
