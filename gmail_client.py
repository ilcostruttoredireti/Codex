import os
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"


class GmailClient:
    def __init__(self):
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(CREDENTIALS_FILE):
                    raise FileNotFoundError(
                        f"File '{CREDENTIALS_FILE}' non trovato.\n"
                        "Scaricalo da Google Cloud Console > APIs & Services > Credentials."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_current_history_id(self):
        profile = self.service.users().getProfile(userId="me").execute()
        return profile.get("historyId")

    def get_new_messages_since(self, history_id):
        """
        Returns (new_messages, latest_history_id) using the History API.
        Falls back to empty list if historyId is too stale (>7 days).
        """
        try:
            response = (
                self.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            messages = []
            for record in response.get("history", []):
                for msg in record.get("messagesAdded", []):
                    messages.append(msg["message"]["id"])

            return messages, response.get("historyId", history_id)

        except HttpError as e:
            if e.resp.status == 404:
                # historyId is too old; return empty and reset to current
                return [], self.get_current_history_id()
            raise

    def get_recent_message_ids(self, max_results=10):
        """Fetches the most recent INBOX message IDs (used on first run)."""
        result = (
            self.service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in result.get("messages", [])]

    def get_message_sender(self, message_id):
        """
        Returns a dict with keys: email, name, domain, subject, message_id.
        Returns None if the message cannot be read.
        """
        try:
            msg = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
            )
        except HttpError:
            return None

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        from_header = headers.get("From", "")

        name, email = parseaddr(from_header)
        email = email.lower().strip()
        if not email or "@" not in email:
            return None

        domain = email.split("@")[1]
        name = name.strip().strip('"')

        return {
            "email": email,
            "name": name,
            "domain": domain,
            "subject": headers.get("Subject", "(no subject)"),
            "message_id": message_id,
        }
