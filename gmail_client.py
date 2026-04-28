"""Gmail API client — fetches new messages and tracks processed history."""

import base64
import email as email_lib
import json
import os
from pathlib import Path
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(".gmail_state.json")


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_credentials(credentials_path: str, token_path: str) -> Credentials:
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json())
    return creds


def _extract_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _parse_sender(from_header: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a From: header value."""
    parsed = email_lib.utils.parseaddr(from_header)
    return parsed[0].strip(), parsed[1].strip().lower()


def _decode_body(payload: dict) -> str:
    """Best-effort extraction of plain-text body."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        text = _decode_body(part)
        if text:
            return text
    return ""


class GmailClient:
    def __init__(self, credentials_path: str = "credentials.json", token_path: str = "token.json"):
        creds = _get_credentials(credentials_path, token_path)
        self._service = build("gmail", "v1", credentials=creds)
        self._state = _load_state()

    def _get_history_id(self) -> str | None:
        return self._state.get("history_id")

    def _set_history_id(self, history_id: str) -> None:
        self._state["history_id"] = history_id
        _save_state(self._state)

    def _get_processed_ids(self) -> set[str]:
        return set(self._state.get("processed_ids", []))

    def _mark_processed(self, msg_id: str) -> None:
        ids = self._get_processed_ids()
        ids.add(msg_id)
        # keep only the last 10 000 to avoid unbounded growth
        self._state["processed_ids"] = list(ids)[-10_000:]
        _save_state(self._state)

    def _fetch_message_detail(self, msg_id: str) -> dict | None:
        try:
            return (
                self._service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except HttpError:
            return None

    def _initial_history_id(self) -> str:
        """Grab the historyId from the user's latest message to bootstrap."""
        result = (
            self._service.users()
            .messages()
            .list(userId="me", maxResults=1)
            .execute()
        )
        messages = result.get("messages", [])
        if messages:
            msg = self._fetch_message_detail(messages[0]["id"])
            return msg["historyId"] if msg else "1"
        return "1"

    def new_messages(self) -> Iterator[dict]:
        """
        Yield new inbound messages since the last poll.

        Each yielded dict contains:
            id, from_name, from_email, subject, snippet, body
        """
        history_id = self._get_history_id()

        if history_id is None:
            # First run: set baseline, yield nothing
            self._set_history_id(self._initial_history_id())
            return

        try:
            history_result = (
                self._service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId expired; reset
                self._set_history_id(self._initial_history_id())
                return
            raise

        new_history_id = history_result.get("historyId", history_id)
        self._set_history_id(new_history_id)

        processed = self._get_processed_ids()
        for record in history_result.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_meta = added.get("message", {})
                msg_id = msg_meta.get("id", "")
                labels = msg_meta.get("labelIds", [])

                # Only process messages that are in INBOX and not sent by us
                if msg_id in processed:
                    continue
                if "INBOX" not in labels:
                    continue
                if "SENT" in labels:
                    continue

                detail = self._fetch_message_detail(msg_id)
                if detail is None:
                    continue

                headers = detail.get("payload", {}).get("headers", [])
                from_header = _extract_header(headers, "From")
                from_name, from_email = _parse_sender(from_header)
                subject = _extract_header(headers, "Subject")
                body = _decode_body(detail.get("payload", {}))

                self._mark_processed(msg_id)

                yield {
                    "id": msg_id,
                    "from_name": from_name,
                    "from_email": from_email,
                    "subject": subject,
                    "snippet": detail.get("snippet", ""),
                    "body": body[:2000],
                }
