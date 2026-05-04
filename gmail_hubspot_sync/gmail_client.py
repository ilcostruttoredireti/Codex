"""Gmail API client — fetches new messages and marks them as processed."""

import email.utils
from pathlib import Path

# Google libraries are imported lazily inside GmailClient.__init__ so that
# pure helper functions (_split_name, etc.) can be unit-tested without
# requiring the full google-auth / cryptography stack to be importable.

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _get_credentials(credentials_file: str, token_file: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

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

    return creds


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        from googleapiclient.discovery import build

        creds = _get_credentials(credentials_file, token_file)
        self._service = build("gmail", "v1", credentials=creds)

    def fetch_unread(self, query: str = "is:unread in:inbox") -> list[dict]:
        """Return a list of parsed sender records from unread messages."""
        from googleapiclient.errors import HttpError

        try:
            result = (
                self._service.users()
                .messages()
                .list(userId="me", q=query)
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"Gmail list error: {exc}") from exc

        messages = result.get("messages", [])
        senders = []
        for msg_ref in messages:
            sender = self._parse_message(msg_ref["id"])
            if sender:
                senders.append(sender)
        return senders

    def mark_as_read(self, message_id: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except HttpError as exc:
            raise RuntimeError(f"Gmail modify error: {exc}") from exc

    def _parse_message(self, message_id: str) -> dict | None:
        from googleapiclient.errors import HttpError

        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError:
            return None

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        from_header = headers.get("From", "")
        if not from_header:
            return None

        name, addr = email.utils.parseaddr(from_header)
        addr = addr.strip().lower()
        if not addr or "@" not in addr:
            return None

        domain = addr.split("@")[1]
        first_name, last_name = _split_name(name.strip())

        return {
            "message_id": message_id,
            "email": addr,
            "full_name": name.strip(),
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
        }


def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Returns (full_name, '') if unclear."""
    parts = full_name.split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return full_name, ""
