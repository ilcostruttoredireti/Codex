import os
import re
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self):
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        token_path = os.getenv("GMAIL_TOKEN_PATH", "token.json")
        credentials_path = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_new_messages(self, after_timestamp_seconds: float = None) -> list[dict]:
        """Return list of {id, threadId} for inbox messages newer than after_timestamp_seconds."""
        query = "in:inbox"
        if after_timestamp_seconds:
            query += f" after:{int(after_timestamp_seconds)}"

        response = self.service.users().messages().list(userId="me", q=query).execute()
        return response.get("messages", [])

    def get_message_details(self, message_id: str) -> dict:
        """Return parsed sender details for a Gmail message ID."""
        msg = self.service.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["From", "Subject", "Date"]
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        sender_email, sender_name = self._parse_from(headers.get("From", ""))

        return {
            "id": message_id,
            "from_email": sender_email.lower(),
            "from_name": sender_name,
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "internal_date_ms": int(msg.get("internalDate", 0)),
        }

    @staticmethod
    def _parse_from(from_header: str) -> tuple[str, str]:
        """Parse 'Name <email>' or bare 'email' into (email, name)."""
        match = re.match(r'"?([^"<]+?)"?\s*<([^>]+)>', from_header.strip())
        if match:
            return match.group(2).strip(), match.group(1).strip()
        bare = re.search(r"[\w.+-]+@[\w.-]+\.\w+", from_header)
        if bare:
            return bare.group(0), ""
        return from_header.strip(), ""
