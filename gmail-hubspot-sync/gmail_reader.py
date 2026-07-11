"""
Gmail API client - fetches unread inbox messages and returns structured dicts.
Requires OAuth2 credentials (credentials.json) and caches the token in token.pickle.
"""
import os
import pickle

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailReader:
    def __init__(self, config) -> None:
        self._creds_path = config.gmail_credentials_path
        self._token_path = config.gmail_token_path
        self._service = self._authenticate()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_unread_messages(self, query: str, max_results: int = 200) -> list[dict]:
        """
        Return a list of message dicts for all messages matching *query*.
        Each dict contains: id, sender, subject, date, labels.
        """
        messages = []
        page_token = None

        while len(messages) < max_results:
            batch_size = min(50, max_results - len(messages))
            kwargs = dict(userId="me", q=query, maxResults=batch_size)
            if page_token:
                kwargs["pageToken"] = page_token

            result = self._service.users().messages().list(**kwargs).execute()
            items = result.get("messages", [])

            for item in items:
                msg = self._fetch_headers(item["id"])
                if msg:
                    messages.append(msg)

            page_token = result.get("nextPageToken")
            if not page_token or not items:
                break

        return messages

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _authenticate(self):
        creds = None
        if os.path.exists(self._token_path):
            with open(self._token_path, "rb") as fh:
                creds = pickle.load(fh)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._creds_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(self._token_path, "wb") as fh:
                pickle.dump(creds, fh)

        return build("gmail", "v1", credentials=creds)

    def _fetch_headers(self, message_id: str) -> dict | None:
        try:
            msg = self._service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {
                h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            return {
                "id": message_id,
                "sender": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "labels": msg.get("labelIds", []),
            }
        except Exception as exc:
            print(f"  [warn] Could not fetch message {message_id}: {exc}")
            return None
