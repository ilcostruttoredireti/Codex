import re
from dataclasses import dataclass

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config


@dataclass
class SenderInfo:
    email: str
    name: str | None
    domain: str
    subject: str
    message_id: str
    thread_id: str


def _build_service():
    creds = Credentials(
        token=None,
        refresh_token=config.GOOGLE_REFRESH_TOKEN,
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


_NAME_EMAIL_RE = re.compile(r'^"?([^"<]+?)"?\s*<([^>]+)>$')


def _parse_sender(raw: str) -> tuple[str | None, str]:
    """Return (display_name, email_address) from a From: header value."""
    raw = raw.strip()
    m = _NAME_EMAIL_RE.match(raw)
    if m:
        name = m.group(1).strip().strip('"') or None
        email = m.group(2).strip().lower()
    else:
        name = None
        email = raw.lower()
    return name, email


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def fetch_new_senders(processed_ids: set[str]) -> list[SenderInfo]:
    """Return SenderInfo for every inbound message not yet in processed_ids."""
    service = _build_service()
    results = (
        service.users()
        .messages()
        .list(userId="me", q=config.GMAIL_QUERY, maxResults=100)
        .execute()
    )
    messages = results.get("messages", [])

    senders: list[SenderInfo] = []
    for msg_stub in messages:
        mid = msg_stub["id"]
        if mid in processed_ids:
            continue

        msg = (
            service.users()
            .messages()
            .get(userId="me", id=mid, format="metadata",
                 metadataHeaders=["From", "Subject"])
            .execute()
        )
        headers = msg.get("payload", {}).get("headers", [])
        raw_from = _header(headers, "From")
        subject = _header(headers, "Subject")

        if not raw_from:
            continue

        name, email = _parse_sender(raw_from)
        domain = email.split("@")[-1] if "@" in email else ""

        if email in config.SKIP_SENDERS:
            continue
        if domain in config.SKIP_DOMAINS:
            continue

        senders.append(
            SenderInfo(
                email=email,
                name=name,
                domain=domain,
                subject=subject,
                message_id=mid,
                thread_id=msg.get("threadId", ""),
            )
        )

    return senders
