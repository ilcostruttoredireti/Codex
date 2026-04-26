"""
Gmail client: OAuth2 authentication and incremental inbox polling.
Uses the Gmail History API to fetch only new messages since the last run,
falling back to a full unread scan on first run.
"""

import os
import json
import logging
import base64
import email as email_lib
from email.header import decode_header, make_header
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str, user: str = "me"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.user = user
        self.service = self._authenticate()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _authenticate(self):
        creds = None
        token_path = Path(self.token_file)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("Gmail token refreshed.")
            else:
                if not Path(self.credentials_file).exists():
                    raise FileNotFoundError(
                        f"Gmail credentials file not found: {self.credentials_file}\n"
                        "Download it from Google Cloud Console → APIs & Services → Credentials."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
                logger.info("Gmail OAuth2 flow completed.")

            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Fetch new messages
    # ------------------------------------------------------------------

    def get_profile(self) -> dict:
        return self.service.users().getProfile(userId=self.user).execute()

    def get_new_messages(self, history_id: Optional[str] = None) -> tuple[list[dict], str]:
        """
        Returns (messages, new_history_id).
        If history_id is None, does a full fetch of recent unread messages.
        """
        profile = self.get_profile()
        current_history_id = profile["historyId"]

        if history_id is None:
            messages = self._list_recent_messages()
            return messages, current_history_id

        try:
            messages = self._list_messages_from_history(history_id)
            return messages, current_history_id
        except HttpError as e:
            if e.resp.status == 404:
                # History token expired; fall back to full scan
                logger.warning("History ID expired, falling back to full unread scan.")
                messages = self._list_recent_messages()
                return messages, current_history_id
            raise

    def _list_recent_messages(self, max_results: int = 100) -> list[dict]:
        """Fetch up to max_results recent inbox messages."""
        result = (
            self.service.users()
            .messages()
            .list(userId=self.user, labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        return self._fetch_message_details(result.get("messages", []))

    def _list_messages_from_history(self, history_id: str) -> list[dict]:
        """Fetch messages added to INBOX since history_id."""
        new_messages = []
        page_token = None

        while True:
            kwargs = {
                "userId": self.user,
                "startHistoryId": history_id,
                "historyTypes": ["messageAdded"],
                "labelId": "INBOX",
            }
            if page_token:
                kwargs["pageToken"] = page_token

            result = self.service.users().history().list(**kwargs).execute()
            history = result.get("history", [])

            for record in history:
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    labels = msg.get("labelIds", [])
                    if "INBOX" in labels:
                        new_messages.append(msg)

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return self._fetch_message_details(new_messages)

    def _fetch_message_details(self, message_stubs: list[dict]) -> list[dict]:
        """Fetch full message metadata for each stub."""
        messages = []
        for stub in message_stubs:
            try:
                msg = (
                    self.service.users()
                    .messages()
                    .get(userId=self.user, id=stub["id"], format="metadata",
                         metadataHeaders=["From", "Subject", "Date"])
                    .execute()
                )
                messages.append(msg)
            except HttpError as e:
                logger.warning("Could not fetch message %s: %s", stub.get("id"), e)
        return messages

    # ------------------------------------------------------------------
    # Sender extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_sender(message: dict) -> dict:
        """
        Parse the From: header and return:
            {email, name, first_name, last_name, domain, company}
        """
        headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")

        name, raw_email = _parse_from_header(from_header)
        raw_email = raw_email.lower().strip()

        first_name, last_name = _split_name(name)
        domain = raw_email.split("@")[-1] if "@" in raw_email else ""
        company = _domain_to_company(domain)

        return {
            "email": raw_email,
            "name": name,
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "company": company,
            "message_id": message.get("id", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_COMMON_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "live.com", "aol.com", "msn.com", "protonmail.com", "me.com",
    "googlemail.com", "ymail.com", "mail.com", "zoho.com",
}

_DOMAIN_PREFIXES = {"mail", "smtp", "email", "info", "contact", "support", "noreply", "no-reply"}


def _parse_from_header(from_header: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a From: header value."""
    try:
        parsed = email_lib.utils.parseaddr(from_header)
        raw_name, addr = parsed
        # Decode RFC2047 encoded name
        if raw_name:
            name = str(make_header(decode_header(raw_name)))
        else:
            name = ""
        return name.strip(), addr.strip()
    except Exception:
        return "", from_header.strip()


def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'John Doe' into ('John', 'Doe'). Handles single names too."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _domain_to_company(domain: str) -> str:
    """
    Convert an email domain to a company name.
    Returns empty string for common free email providers.
    """
    if not domain or domain in _COMMON_DOMAINS:
        return ""

    # Strip leading subdomain prefixes
    parts = domain.split(".")
    if len(parts) > 2 and parts[0].lower() in _DOMAIN_PREFIXES:
        parts = parts[1:]

    # Use the second-level domain as company name
    company_raw = parts[0] if parts else domain
    return company_raw.capitalize()
