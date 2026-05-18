import os
import re
import logging
from email.header import decode_header as _decode_header

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Local parts that indicate automated / no-reply senders
_SKIP_PREFIXES = (
    "noreply@",
    "no-reply@",
    "donotreply@",
    "do-not-reply@",
    "mailer-daemon@",
    "postmaster@",
    "bounce@",
    "bounces@",
    "notifications@",
    "automated@",
    "system@",
    "newsletter@",
    "unsubscribe@",
)


class GmailClient:
    def __init__(self, credentials_file="credentials.json", token_file="token.json"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()
        self._user_email: str | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

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
            with open(self.token_file, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def user_email(self) -> str:
        if not self._user_email:
            profile = self.service.users().getProfile(userId="me").execute()
            self._user_email = profile.get("emailAddress", "").lower()
        return self._user_email

    def get_messages(self, after_epoch: int = None, max_results: int = 50) -> list:
        query = "in:inbox"
        if after_epoch:
            query += f" after:{int(after_epoch)}"

        try:
            result = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            return result.get("messages", [])
        except HttpError as exc:
            logger.error("Gmail API error listing messages: %s", exc)
            return []

    def get_message_detail(self, message_id: str) -> dict | None:
        try:
            return (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date", "Message-ID"],
                )
                .execute()
            )
        except HttpError as exc:
            logger.error("Gmail API error getting message %s: %s", message_id, exc)
            return None

    def parse_sender(self, message: dict) -> dict | None:
        headers = {
            h["name"]: h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }

        from_header = headers.get("From", "")
        subject = headers.get("Subject", "")
        date = headers.get("Date", "")

        name, email_addr = self._parse_from_header(from_header)

        if not email_addr or "@" not in email_addr:
            return None

        email_addr = email_addr.lower().strip()

        # Skip own address
        if email_addr == self.user_email:
            return None

        # Skip automated senders
        if any(email_addr.startswith(prefix) for prefix in _SKIP_PREFIXES):
            logger.debug("Skipping automated sender: %s", email_addr)
            return None

        domain = email_addr.split("@")[1]

        return {
            "message_id": message["id"],
            "internal_date": int(message.get("internalDate", 0)),
            "name": name,
            "email": email_addr,
            "domain": domain,
            "subject": subject,
            "date": date,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_from_header(self, from_header: str) -> tuple[str, str]:
        match = re.match(r'^(.*?)\s*<([^>]+)>\s*$', from_header.strip())
        if match:
            raw_name = match.group(1).strip().strip("\"'")
            email_addr = match.group(2).strip()
            return self._decode_mime(raw_name), email_addr

        stripped = from_header.strip()
        if "@" in stripped:
            return "", stripped
        return "", ""

    @staticmethod
    def _decode_mime(value: str) -> str:
        try:
            parts = _decode_header(value)
            return "".join(
                chunk.decode(enc or "utf-8", errors="replace")
                if isinstance(chunk, bytes)
                else chunk
                for chunk, enc in parts
            ).strip()
        except Exception:
            return value
