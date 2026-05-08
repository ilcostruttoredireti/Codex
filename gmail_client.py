"""Gmail client: OAuth 2.0 authentication and inbox polling."""

import os
from email.header import decode_header, make_header

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from utils import parse_sender, extract_name_parts

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _decode_header_value(raw: str) -> str:
    return str(make_header(decode_header(raw))) if raw else ""


def build_gmail_service(credentials_file: str, token_file: str):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# Expose private-style aliases so existing callers continue to work
_parse_sender = parse_sender
_extract_name_parts = extract_name_parts


def fetch_new_messages(service, history_id: str | None) -> tuple[list[dict], str]:
    """
    Poll Gmail for messages newer than history_id.

    Returns (messages, new_history_id).
    Each message dict: {id, from_header, sender, subject, date}.
    """
    profile = service.users().getProfile(userId="me").execute()
    new_history_id = profile["historyId"]

    if history_id is None:
        # First run: return nothing, just record the current cursor.
        return [], new_history_id

    if history_id == new_history_id:
        return [], new_history_id

    try:
        response = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            .execute()
        )
    except Exception:
        # historyId expired – fall back to listing recent messages
        return _fallback_recent(service, new_history_id)

    messages = []
    for record in response.get("history", []):
        for msg_added in record.get("messagesAdded", []):
            msg_meta = msg_added["message"]
            # Only process INBOX messages
            if "INBOX" not in msg_meta.get("labelIds", []):
                continue
            detail = _get_message_detail(service, msg_meta["id"])
            if detail:
                messages.append(detail)

    return messages, new_history_id


def _fallback_recent(service, new_history_id: str) -> tuple[list[dict], str]:
    """Return the 10 most recent INBOX messages as a fallback."""
    result = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=10)
        .execute()
    )
    messages = []
    for item in result.get("messages", []):
        detail = _get_message_detail(service, item["id"])
        if detail:
            messages.append(detail)
    return messages, new_history_id


def _get_message_detail(service, msg_id: str) -> dict | None:
    """Fetch full message and return a simplified dict."""
    msg = service.users().messages().get(userId="me", id=msg_id, format="metadata",
                                         metadataHeaders=["From", "Subject", "Date"]).execute()

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    from_raw = headers.get("From", "")
    if not from_raw:
        return None

    from_raw = _decode_header_value(from_raw)
    sender = _parse_sender(from_raw)
    first, last = _extract_name_parts(sender["name"])

    return {
        "id": msg_id,
        "from_header": from_raw,
        "sender": {
            "email": sender["email"],
            "first_name": first,
            "last_name": last,
            "domain": sender["domain"],
        },
        "subject": _decode_header_value(headers.get("Subject", "")),
        "date": headers.get("Date", ""),
    }
