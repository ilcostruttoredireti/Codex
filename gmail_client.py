"""Gmail API client: authentication and inbox polling via History API."""

import os
import json
import logging
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domains whose emails we silently discard (automated senders)
_SKIP_LOCAL_PARTS = frozenset(
    ["noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
     "postmaster", "bounce", "bounces", "notifications", "newsletter"]
)

# Free consumer email providers — company name is not inferrable from domain
_CONSUMER_DOMAINS = frozenset([
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "outlook.com", "hotmail.com", "hotmail.it", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "aol.com", "libero.it", "tiscali.it", "virgilio.it", "alice.it",
    "fastwebnet.it", "tin.it",
])


def build_service(credentials_file: str, token_file: str = "token.json"):
    """Return an authenticated Gmail service, refreshing or re-authorising as needed."""
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


# ---------------------------------------------------------------------------
# History-based incremental polling
# ---------------------------------------------------------------------------

def get_initial_history_id(service) -> str:
    """Return the current inbox historyId so subsequent polls fetch only new mail."""
    profile = service.users().getProfile(userId="me").execute()
    return profile["historyId"]


def fetch_new_message_ids(service, start_history_id: str) -> tuple[list[str], str]:
    """
    Fetch message IDs added to the inbox since *start_history_id*.

    Returns (message_ids, latest_history_id).
    """
    new_ids: list[str] = []
    latest_id = start_history_id

    try:
        resp = service.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
        ).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            # historyId expired — reset to current
            logger.warning("historyId expired, resetting to current inbox state.")
            latest_id = get_initial_history_id(service)
            return [], latest_id
        raise

    for record in resp.get("history", []):
        for added in record.get("messagesAdded", []):
            msg = added["message"]
            if "INBOX" in msg.get("labelIds", []):
                new_ids.append(msg["id"])

    if "historyId" in resp:
        latest_id = resp["historyId"]

    # Follow pagination
    page_token = resp.get("nextPageToken")
    while page_token:
        resp = service.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
            pageToken=page_token,
        ).execute()
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added["message"]
                if "INBOX" in msg.get("labelIds", []):
                    new_ids.append(msg["id"])
        if "historyId" in resp:
            latest_id = resp["historyId"]
        page_token = resp.get("nextPageToken")

    return new_ids, latest_id


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def parse_sender(service, msg_id: str) -> dict | None:
    """
    Fetch message headers and return a sender dict or None if the message
    should be ignored (automated sender, missing address, …).
    """
    try:
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
    except HttpError as exc:
        logger.warning("Could not fetch message %s: %s", msg_id, exc)
        return None

    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    from_header = headers.get("From", "")
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()

    if not email_addr or "@" not in email_addr:
        return None

    local_part = email_addr.split("@")[0]
    if any(skip in local_part for skip in _SKIP_LOCAL_PARTS):
        logger.debug("Skipping automated sender: %s", email_addr)
        return None

    domain = email_addr.split("@")[1]
    company = _infer_company(domain)

    first_name, last_name = _split_display_name(display_name.strip())

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
        "subject": headers.get("Subject", ""),
        "message_id": msg_id,
    }


def _split_display_name(name: str) -> tuple[str, str]:
    parts = name.split(" ", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _infer_company(domain: str) -> str:
    """Convert a corporate domain to a human-readable company name."""
    if domain in _CONSUMER_DOMAINS:
        return ""
    # "acme-corp.com" → "Acme Corp"
    stem = domain.split(".")[0]
    return stem.replace("-", " ").replace("_", " ").title()
