"""Gmail client: authenticates via OAuth2 and fetches new inbound messages."""

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from utils import parse_sender

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _build_service(credentials_file: str, token_file: str):
    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _get_or_create_label(service, label_name: str) -> str:
    """Return the label id, creating the label if it doesn't exist."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == label_name:
            return lbl["id"]
    body = {"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
    created = service.users().labels().create(userId="me", body=body).execute()
    return created["id"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str, processed_label: str = ""):
        self._service = _build_service(credentials_file, token_file)
        self._processed_label_name = processed_label
        self._processed_label_id: str | None = None
        if processed_label:
            self._processed_label_id = _get_or_create_label(self._service, processed_label)

    def fetch_new_messages(self, after_history_id: str | None = None) -> tuple[list[dict], str]:
        """
        Return (messages, latest_history_id).

        On first run (after_history_id is None) returns messages from the last
        24 hours so the backlog is manageable.
        """
        if after_history_id is None:
            # Bootstrap: fetch INBOX messages not yet labelled as processed
            query = "in:inbox"
            if self._processed_label_name:
                query += f" -label:{self._processed_label_name}"
            result = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=50)
                .execute()
            )
            msg_stubs = result.get("messages", [])
            messages = [self._fetch_message(m["id"]) for m in msg_stubs]
            # Retrieve current historyId for incremental polling going forward
            profile = self._service.users().getProfile(userId="me").execute()
            history_id = profile["historyId"]
            return messages, history_id

        # Incremental: use history API
        messages = []
        try:
            history = (
                self._service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=after_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except Exception:
            # historyId too old — fall back to bootstrap behaviour
            return self.fetch_new_messages(after_history_id=None)

        latest_history_id = history.get("historyId", after_history_id)
        for record in history.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                if self._processed_label_id and self._processed_label_id in added["message"].get("labelIds", []):
                    continue
                messages.append(self._fetch_message(msg_id))

        return messages, latest_history_id

    def _fetch_message(self, msg_id: str) -> dict:
        raw = (
            self._service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata", metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in raw.get("payload", {}).get("headers", [])}
        sender = parse_sender(headers.get("From", ""))
        return {
            "msg_id": msg_id,
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "sender": sender,
        }

    def mark_processed(self, msg_id: str) -> None:
        """Apply the processed label to a message so it isn't re-processed."""
        if not self._processed_label_id:
            return
        self._service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": [self._processed_label_id]},
        ).execute()
