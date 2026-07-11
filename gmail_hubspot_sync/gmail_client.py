import json
import os
from datetime import datetime, timedelta, timezone
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import Config
from .models import ContactInfo
from .parser import parse_sender


class GmailClient:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._service = None

    def _authenticate(self):
        creds = None
        token_file = self._cfg.gmail_token_file

        if os.path.exists(token_file):
            creds = Credentials.from_authorized_user_file(token_file, self._cfg.gmail_scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._cfg.gmail_credentials_file, self._cfg.gmail_scopes
                )
                creds = flow.run_local_server(port=0)
            with open(token_file, "w") as f:
                f.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)

    def _build_query(self, after_timestamp: int | None) -> str:
        parts = ["in:inbox", "-from:me"]
        if after_timestamp:
            dt = datetime.fromtimestamp(after_timestamp, tz=timezone.utc)
            parts.append(f"after:{int(dt.timestamp())}")
        else:
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self._cfg.lookback_days)
            parts.append(f"after:{int(cutoff.timestamp())}")
        return " ".join(parts)

    def fetch_new_senders(self, after_timestamp: int | None = None) -> Generator[ContactInfo, None, None]:
        if not self._service:
            self._authenticate()

        query = self._build_query(after_timestamp)
        seen_emails: set[str] = set()
        page_token = None

        while True:
            kwargs = {"userId": "me", "q": query, "maxResults": 100}
            if page_token:
                kwargs["pageToken"] = page_token

            response = self._service.users().messages().list(**kwargs).execute()
            messages = response.get("messages", [])

            for msg_ref in messages:
                msg = (
                    self._service.users()
                    .messages()
                    .get(userId="me", id=msg_ref["id"], format="metadata",
                         metadataHeaders=["From", "Subject", "Date"])
                    .execute()
                )
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                raw_from = headers.get("From", "")
                subject = headers.get("Subject", "")
                date = headers.get("Date", "")

                contact = parse_sender(raw_from, subject=subject, date=date)
                if contact and contact.email not in seen_emails:
                    seen_emails.add(contact.email)
                    yield contact

            page_token = response.get("nextPageToken")
            if not page_token:
                break
