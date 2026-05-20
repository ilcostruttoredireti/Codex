import os
import pickle
import logging
from email.utils import parseaddr

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailClient:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.pickle"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            with open(self.token_file, "rb") as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "wb") as f:
                pickle.dump(creds, f)

        return build("gmail", "v1", credentials=creds)

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def get_new_message_ids(self, history_id: str) -> tuple[list[str], str]:
        """
        Returns (list_of_new_inbox_message_ids, updated_history_id).
        Falls back to a recent-messages scan if the historyId has expired (404).
        """
        try:
            resp = self.service.users().history().list(
                userId="me",
                startHistoryId=history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()

            ids = []
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    if "INBOX" in msg.get("labelIds", []):
                        ids.append(msg["id"])

            return ids, resp.get("historyId", history_id)

        except Exception as exc:
            # historyId expired → fall back to listing recent inbox messages
            if "404" in str(exc) or "Invalid" in str(exc):
                logger.warning("historyId scaduto, recupero messaggi recenti come fallback.")
                return self._list_recent_inbox_ids(max_results=50)
            raise

    def _list_recent_inbox_ids(self, max_results: int = 50) -> tuple[list[str], str]:
        resp = self.service.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=max_results,
        ).execute()
        ids = [m["id"] for m in resp.get("messages", [])]
        new_history_id = self.get_current_history_id()
        return ids, new_history_id

    def get_message_sender(self, msg_id: str) -> dict | None:
        """Returns sender metadata dict or None on error."""
        try:
            msg = self.service.users().messages().get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()

            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            from_header = headers.get("From", "")
            name, email = parseaddr(from_header)

            return {
                "id": msg_id,
                "name": name.strip(),
                "email": email.strip().lower(),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            }
        except Exception as exc:
            logger.error(f"Impossibile leggere messaggio {msg_id}: {exc}")
            return None
