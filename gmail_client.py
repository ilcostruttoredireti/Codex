import os
from email.utils import parseaddr
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def ensure_label(self, label_name: str) -> str:
        """Return the label ID, creating the label if it does not exist."""
        result = self.service.users().labels().list(userId="me").execute()
        for label in result.get("labels", []):
            if label["name"] == label_name:
                return label["id"]

        created = (
            self.service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": label_name,
                    "labelListVisibility": "labelHide",
                    "messageListVisibility": "hide",
                },
            )
            .execute()
        )
        return created["id"]

    def list_unsynced(self, synced_label: str, lookback_days: int = 7) -> list[dict]:
        """Return inbox messages that have not been labelled as synced yet."""
        q = f"in:inbox -label:{synced_label}"
        if lookback_days > 0:
            q += f" newer_than:{lookback_days}d"

        messages: list[dict] = []
        page_token = None

        while True:
            params: dict = {"userId": "me", "q": q, "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token

            resp = self.service.users().messages().list(**params).execute()
            messages.extend(resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return messages

    def get_sender(self, message_id: str) -> dict:
        """Fetch the From/Subject/Date headers for a single message."""
        msg = (
            self.service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        raw_from = headers.get("From", "")
        name, email = parseaddr(raw_from)

        return {
            "message_id": message_id,
            "email": email.lower().strip(),
            "name": name.strip(),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
        }

    def mark_synced(self, message_id: str, label_id: str) -> None:
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()
