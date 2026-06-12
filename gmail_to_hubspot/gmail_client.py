import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _get_credentials(credentials_path: str, token_path: str) -> Credentials:
    creds = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, _SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, _SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json())
    return creds


def build_service(credentials_path: str, token_path: str):
    return build("gmail", "v1", credentials=_get_credentials(credentials_path, token_path))


def _get_history_id(service) -> str:
    return service.users().getProfile(userId="me").execute().get("historyId", "")


def fetch_initial_messages(service, lookback_days: int = 7) -> tuple[list[dict], str]:
    """First-run scan: returns messages from the last N days and the current historyId."""
    result = service.users().messages().list(
        userId="me",
        maxResults=500,
        q=f"in:inbox newer_than:{lookback_days}d",
    ).execute()
    history_id = _get_history_id(service)
    return result.get("messages", []), history_id


def fetch_messages_since(service, history_id: str) -> tuple[list[dict], str]:
    """Incremental fetch using Gmail History API. Falls back to initial scan if historyId expired."""
    try:
        resp = service.users().history().list(
            userId="me",
            startHistoryId=history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
        ).execute()
    except Exception as exc:
        logger.warning("Gmail history ID scaduto (%s), ripartenza dalla scansione iniziale.", exc)
        return fetch_initial_messages(service)

    messages = [
        {"id": added["message"]["id"]}
        for record in resp.get("history", [])
        for added in record.get("messagesAdded", [])
    ]
    return messages, resp.get("historyId", history_id)


def get_message_headers(service, message_id: str) -> dict:
    msg = service.users().messages().get(
        userId="me",
        id=message_id,
        format="metadata",
        metadataHeaders=["From", "Subject"],
    ).execute()
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
