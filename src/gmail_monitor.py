import os
import json
import logging
from pathlib import Path

from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

log = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


class GmailMonitor:
    def __init__(self):
        self.credentials_file = os.environ.get('GMAIL_CREDENTIALS_FILE', 'credentials.json')
        self.token_file = os.environ.get('GMAIL_TOKEN_FILE', 'token.json')
        self.state_file = os.environ.get('GMAIL_STATE_FILE', '.gmail_state.json')
        self.user_id = 'me'
        self.service = self._authenticate()
        self.last_history_id = self._init_history_id()

    def _authenticate(self):
        creds = None
        if Path(self.token_file).exists():
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not Path(self.credentials_file).exists():
                    raise FileNotFoundError(
                        f"File credenziali Gmail non trovato: {self.credentials_file}\n"
                        "Scaricalo da Google Cloud Console (OAuth 2.0 Client ID)."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, 'w') as f:
                f.write(creds.to_json())
            log.info("Autenticazione Gmail completata, token salvato.")
        return build('gmail', 'v1', credentials=creds)

    def _init_history_id(self):
        if Path(self.state_file).exists():
            with open(self.state_file) as f:
                data = json.load(f)
                hid = data.get('last_history_id')
                if hid:
                    log.info(f"Ripresa dal history ID: {hid}")
                    return hid
        # Primo avvio: usa l'ID corrente, non processare email vecchie
        profile = self.service.users().getProfile(userId=self.user_id).execute()
        hid = profile['historyId']
        self._save_state(hid)
        log.info(f"Primo avvio, history ID iniziale: {hid}")
        return hid

    def _save_state(self, history_id):
        with open(self.state_file, 'w') as f:
            json.dump({'last_history_id': history_id}, f)
        self.last_history_id = history_id

    def get_new_emails(self):
        emails = []
        try:
            page_token = None
            new_history_id = self.last_history_id

            while True:
                kwargs = dict(
                    userId=self.user_id,
                    startHistoryId=self.last_history_id,
                    historyTypes=['messageAdded'],
                    labelId='INBOX',
                )
                if page_token:
                    kwargs['pageToken'] = page_token

                result = self.service.users().history().list(**kwargs).execute()
                new_history_id = result.get('historyId', new_history_id)

                for item in result.get('history', []):
                    for msg_added in item.get('messagesAdded', []):
                        msg = msg_added.get('message', {})
                        labels = msg.get('labelIds', [])
                        if 'INBOX' in labels and 'SENT' not in labels:
                            email = self._fetch_metadata(msg['id'])
                            if email:
                                emails.append(email)

                page_token = result.get('nextPageToken')
                if not page_token:
                    break

            self._save_state(new_history_id)

        except Exception as e:
            # historyId scaduto (>30 giorni): reset
            if 'historyId' in str(e) or '404' in str(e):
                log.warning("History ID scaduto, reset allo stato corrente.")
                self._reset_history_id()
            else:
                log.error(f"Errore nel recupero della storia Gmail: {e}")

        return emails

    def _reset_history_id(self):
        profile = self.service.users().getProfile(userId=self.user_id).execute()
        self._save_state(profile['historyId'])

    def _fetch_metadata(self, message_id):
        try:
            msg = self.service.users().messages().get(
                userId=self.user_id,
                id=message_id,
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date'],
            ).execute()
            headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
            return {
                'id': message_id,
                'from': headers.get('From', ''),
                'subject': headers.get('Subject', ''),
                'date': headers.get('Date', ''),
            }
        except Exception as e:
            log.error(f"Errore nel recupero email {message_id}: {e}")
            return None
