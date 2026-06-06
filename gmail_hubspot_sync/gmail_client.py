"""Gmail API client — fetches inbox messages and parses sender info."""

import re
import logging
from email.utils import parseaddr
from typing import Optional
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domains that belong to automated senders — skip these.
_SKIP_DOMAINS_DEFAULT = {
    "noreply.com",
    "no-reply.com",
    "mailer-daemon.com",
    "notifications.google.com",
    "bounce.com",
}


def build_service(client_id: str, client_secret: str, refresh_token: str):
    """Return an authenticated Gmail API service."""
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def parse_sender(raw_from: str) -> dict:
    """
    Parse a raw From header into structured fields.

    Returns dict with keys: email, first_name, last_name, display_name, domain.
    """
    display_name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.strip().lower()

    if not email_addr or "@" not in email_addr:
        return {}

    domain = email_addr.split("@")[1]
    first_name = ""
    last_name = ""

    if display_name:
        # Strip surrounding quotes that some clients add
        display_name = display_name.strip('"').strip("'").strip()
        parts = display_name.split(None, 1)
        first_name = parts[0].capitalize() if parts else ""
        last_name = parts[1].capitalize() if len(parts) > 1 else ""

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "display_name": display_name,
        "domain": domain,
    }


def company_from_domain(domain: str, skip_domains: set) -> Optional[str]:
    """Derive a company name from the sender domain (strips TLD and common prefixes)."""
    if domain in skip_domains:
        return None
    # Remove subdomain prefixes like mail., news., em., etc.
    parts = domain.split(".")
    # Take the second-to-last part as the company stem (e.g. "acme" from "mail.acme.com")
    if len(parts) >= 2:
        company_stem = parts[-2]
        return company_stem.capitalize()
    return None


def fetch_new_messages(service, after_history_id: Optional[str], skip_domains: set):
    """
    Yield parsed sender dicts for each new inbox message.

    Uses Gmail historyId-based polling when possible; falls back to a
    recent-messages query on the first run (no history id yet).

    Yields tuples of (message_id, sender_dict).
    """
    user = "me"

    if after_history_id:
        yield from _fetch_via_history(service, user, after_history_id, skip_domains)
    else:
        yield from _fetch_recent_inbox(service, user, skip_domains)


def _fetch_via_history(service, user, start_history_id, skip_domains):
    """Yield new messages from Gmail history since start_history_id."""
    try:
        response = (
            service.users()
            .history()
            .list(
                userId=user,
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            .execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            # History id expired — fall back to recent query
            logger.warning("History ID expired, falling back to recent inbox scan.")
            yield from _fetch_recent_inbox(service, user, skip_domains)
            return
        raise

    history_items = response.get("history", [])
    for item in history_items:
        for msg_added in item.get("messagesAdded", []):
            msg = msg_added.get("message", {})
            label_ids = msg.get("labelIds", [])
            if "INBOX" not in label_ids:
                continue
            msg_id = msg["id"]
            sender = _get_sender_from_message(service, user, msg_id)
            if sender and sender.get("domain") not in skip_domains:
                yield msg_id, sender


def _fetch_recent_inbox(service, user, skip_domains, max_results=50):
    """Yield the most recent inbox messages (used on first run)."""
    try:
        response = (
            service.users()
            .messages()
            .list(
                userId=user,
                labelIds=["INBOX"],
                maxResults=max_results,
                q="newer_than:1d",
            )
            .execute()
        )
    except HttpError:
        logger.exception("Error listing inbox messages.")
        return

    for msg_meta in response.get("messages", []):
        msg_id = msg_meta["id"]
        sender = _get_sender_from_message(service, user, msg_id)
        if sender and sender.get("domain") not in skip_domains:
            yield msg_id, sender


def _get_sender_from_message(service, user, msg_id) -> Optional[dict]:
    """Fetch minimal message metadata and return parsed sender dict."""
    try:
        msg = (
            service.users()
            .messages()
            .get(userId=user, id=msg_id, format="metadata", metadataHeaders=["From"])
            .execute()
        )
    except HttpError:
        logger.exception("Could not fetch message %s", msg_id)
        return None

    headers = msg.get("payload", {}).get("headers", [])
    from_header = next((h["value"] for h in headers if h["name"] == "From"), None)
    if not from_header:
        return None

    return parse_sender(from_header)


def get_current_history_id(service) -> Optional[str]:
    """Return the current historyId from the user's mailbox profile."""
    try:
        profile = service.users().getProfile(userId="me").execute()
        return profile.get("historyId")
    except HttpError:
        logger.exception("Could not fetch Gmail profile.")
        return None
