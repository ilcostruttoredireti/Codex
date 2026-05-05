import os
import logging
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    GMAIL_SCOPES,
    FREE_EMAIL_DOMAINS,
    SKIP_LOCAL_PARTS,
)

logger = logging.getLogger(__name__)


class GmailClient:
    def __init__(self):
        self.service = self._authenticate()
        self._processed_ids: set[str] = set()

    def _authenticate(self):
        creds = None
        if os.path.exists(GMAIL_TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(GMAIL_TOKEN_FILE, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_new_emails(self) -> list[dict]:
        """Return sender data for unread inbox messages not yet processed."""
        try:
            response = (
                self.service.users()
                .messages()
                .list(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=50)
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail list error: %s", exc)
            return []

        new_emails = []
        for ref in response.get("messages", []):
            msg_id = ref["id"]
            if msg_id in self._processed_ids:
                continue

            sender = self._fetch_sender(msg_id)
            if sender:
                new_emails.append(sender)
            self._processed_ids.add(msg_id)

        return new_emails

    def _fetch_sender(self, msg_id: str) -> dict | None:
        try:
            msg = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail get error for %s: %s", msg_id, exc)
            return None

        from_header = self._header(msg, "From")
        if not from_header:
            return None

        return self._parse_sender(
            msg_id=msg_id,
            thread_id=msg.get("threadId", ""),
            from_header=from_header,
            subject=self._header(msg, "Subject"),
            date=self._header(msg, "Date"),
        )

    @staticmethod
    def _header(msg: dict, name: str) -> str:
        for h in msg.get("payload", {}).get("headers", []):
            if h["name"].lower() == name.lower():
                return h["value"]
        return ""

    def _parse_sender(
        self,
        msg_id: str,
        thread_id: str,
        from_header: str,
        subject: str,
        date: str,
    ) -> dict | None:
        display_name, email_addr = parseaddr(from_header)
        if not email_addr or "@" not in email_addr:
            return None

        email_addr = email_addr.lower().strip()
        local_part, domain = email_addr.split("@", 1)

        if any(local_part.startswith(skip) for skip in SKIP_LOCAL_PARTS):
            logger.debug("Skipping automated sender: %s", email_addr)
            return None

        first_name, last_name = self._split_name(display_name)
        company = self._domain_to_company(domain)

        return {
            "msg_id": msg_id,
            "thread_id": thread_id,
            "email": email_addr,
            "display_name": display_name.strip(),
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "company": company,
            "subject": subject,
            "date": date,
        }

    @staticmethod
    def _split_name(display_name: str) -> tuple[str, str]:
        if not display_name:
            return "", ""
        parts = display_name.strip().split(" ", 1)
        return parts[0], parts[1] if len(parts) > 1 else ""

    @staticmethod
    def _domain_to_company(domain: str) -> str:
        if domain in FREE_EMAIL_DOMAINS:
            return ""
        return domain.split(".")[0].replace("-", " ").title()
