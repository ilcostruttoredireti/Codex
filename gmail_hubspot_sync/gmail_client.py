"""Gmail API client: authentication and email fetching."""

import os
from dataclasses import dataclass
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import Config
from utils import domain_to_company, parse_name


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    display_name: str
    domain: str
    company: str
    message_id: str
    thread_id: str


def _get_credentials() -> Credentials:
    creds = None
    if os.path.exists(Config.GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(Config.GMAIL_TOKEN_FILE, Config.GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                Config.GMAIL_CREDENTIALS_FILE, Config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(Config.GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds


def build_gmail_service():
    creds = _get_credentials()
    return build("gmail", "v1", credentials=creds)



def fetch_new_messages(service, history_id: str | None) -> tuple[list[SenderInfo], str]:
    """
    Fetch new messages since *history_id*.
    Returns (list_of_senders, latest_history_id).
    On first run (history_id=None) returns the current historyId only.
    """
    profile = service.users().getProfile(userId="me").execute()
    current_history_id = profile["historyId"]

    if history_id is None:
        return [], current_history_id

    senders: list[SenderInfo] = []
    page_token = None

    while True:
        kwargs: dict = {
            "userId": "me",
            "startHistoryId": history_id,
            "historyTypes": ["messageAdded"],
        }
        if page_token:
            kwargs["pageToken"] = page_token

        try:
            response = service.users().history().list(**kwargs).execute()
        except Exception as exc:
            # historyId expired — treat as first run
            if "Invalid startHistoryId" in str(exc):
                return [], current_history_id
            raise

        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added["message"]
                labels = msg.get("labelIds", [])
                # only process inbox messages (not sent, not drafts)
                if "INBOX" not in labels:
                    continue
                detail = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg["id"], format="metadata",
                         metadataHeaders=["From"])
                    .execute()
                )
                from_header = next(
                    (h["value"] for h in detail.get("payload", {}).get("headers", [])
                     if h["name"] == "From"),
                    None,
                )
                if not from_header:
                    continue

                display_name, email_address = parseaddr(from_header)
                if not email_address or "@" not in email_address:
                    continue

                email_lower = email_address.lower()
                domain = email_lower.split("@")[1]
                first, last = parse_name(display_name)
                company = domain_to_company(domain)

                senders.append(SenderInfo(
                    email=email_lower,
                    first_name=first,
                    last_name=last,
                    display_name=display_name,
                    domain=domain,
                    company=company,
                    message_id=msg["id"],
                    thread_id=msg.get("threadId", ""),
                ))

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return senders, current_history_id
