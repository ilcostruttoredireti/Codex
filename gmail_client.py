import os
import pickle
import logging
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
TOKEN_PATH = os.getenv('GMAIL_TOKEN_PATH', 'token.pickle')
CREDENTIALS_PATH = os.getenv('GMAIL_CREDENTIALS_PATH', 'credentials.json')


class GmailClient:
    def __init__(self):
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None

        if os.path.exists(TOKEN_PATH):
            with open(TOKEN_PATH, 'rb') as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                log.info("Token Gmail aggiornato.")
            else:
                if not os.path.exists(CREDENTIALS_PATH):
                    raise FileNotFoundError(
                        f"File credenziali Gmail non trovato: {CREDENTIALS_PATH}\n"
                        "Scarica credentials.json da Google Cloud Console e posizionalo "
                        "nella directory del progetto, oppure imposta GMAIL_CREDENTIALS_PATH."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
                creds = flow.run_local_server(port=0)
                log.info("Autenticazione Gmail completata.")

            with open(TOKEN_PATH, 'wb') as f:
                pickle.dump(creds, f)

        return build('gmail', 'v1', credentials=creds)

    def list_inbox_messages(self, max_results: int = 100, query: str = 'in:inbox') -> list[str]:
        """Returns a list of message IDs from the inbox."""
        try:
            result = self.service.users().messages().list(
                userId='me',
                q=query,
                maxResults=max_results
            ).execute()
            messages = result.get('messages', [])
            return [m['id'] for m in messages]
        except HttpError as e:
            log.error(f"Errore listing messaggi Gmail: {e}")
            return []

    def get_message_headers(self, message_id: str) -> dict | None:
        """Fetches only From/Subject/Date headers for a message (lightweight)."""
        try:
            msg = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date']
            ).execute()

            headers = {
                h['name']: h['value']
                for h in msg.get('payload', {}).get('headers', [])
            }
            return {
                'id': message_id,
                'from': headers.get('From', ''),
                'subject': headers.get('Subject', ''),
                'date': headers.get('Date', ''),
                'label_ids': msg.get('labelIds', []),
            }
        except HttpError as e:
            log.error(f"Errore recupero messaggio {message_id}: {e}")
            return None
