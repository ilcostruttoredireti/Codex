import email.utils
import logging
from pathlib import Path
from typing import Iterator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
from models import SenderInfo

logger = logging.getLogger(__name__)


class GmailClient:
    def __init__(self) -> None:
        self._service = None
        self._processed_label_id: Optional[str] = None

    def authenticate(self) -> None:
        creds: Optional[Credentials] = None
        token_path = Path(config.GMAIL_TOKEN_FILE)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), config.GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail autenticato con successo")

    @property
    def service(self):
        if not self._service:
            raise RuntimeError("Chiamare prima authenticate()")
        return self._service

    # ── Labels ───────────────────────────────────────────────────────────────

    def get_or_create_processed_label(self) -> str:
        if self._processed_label_id:
            return self._processed_label_id

        labels = self.service.users().labels().list(userId="me").execute()
        for label in labels.get("labels", []):
            if label["name"] == config.PROCESSED_LABEL_NAME:
                self._processed_label_id = label["id"]
                return self._processed_label_id

        new_label = (
            self.service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": config.PROCESSED_LABEL_NAME,
                    "labelListVisibility": "labelHide",
                    "messageListVisibility": "hide",
                },
            )
            .execute()
        )
        self._processed_label_id = new_label["id"]
        logger.info("Label Gmail creata: %s", config.PROCESSED_LABEL_NAME)
        return self._processed_label_id

    # ── History tracking ─────────────────────────────────────────────────────

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def iter_new_messages(self, since_history_id: str) -> Iterator[dict]:
        """Yield new INBOX message stubs added since *since_history_id*."""
        processed_label_id = self.get_or_create_processed_label()
        page_token: Optional[str] = None

        while True:
            params: dict = {
                "userId": "me",
                "startHistoryId": since_history_id,
                "historyTypes": ["messageAdded"],
                "labelId": "INBOX",
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                response = self.service.users().history().list(**params).execute()
            except HttpError as exc:
                if exc.resp.status == 404:
                    logger.warning("History ID scaduto, fallback a ricerca recente")
                    yield from self._iter_recent_unprocessed()
                    return
                raise

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    if "INBOX" in msg.get("labelIds", []) and processed_label_id not in msg.get(
                        "labelIds", []
                    ):
                        yield msg

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    def _iter_recent_unprocessed(self) -> Iterator[dict]:
        """Fallback: search INBOX messages not yet labeled as processed."""
        query = f"in:INBOX newer_than:7d -label:{config.PROCESSED_LABEL_NAME}"
        page_token: Optional[str] = None
        while True:
            resp = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, pageToken=page_token)
                .execute()
            )
            for msg in resp.get("messages", []):
                yield msg
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    # ── Message parsing ───────────────────────────────────────────────────────

    def get_message_sender(self, message_id: str) -> Optional[SenderInfo]:
        try:
            msg = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From"],
                )
                .execute()
            )
        except HttpError:
            logger.exception("Impossibile recuperare il messaggio %s", message_id)
            return None

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "")
        return parse_sender(from_header) if from_header else None

    def mark_processed(self, message_id: str) -> None:
        label_id = self.get_or_create_processed_label()
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> Optional[SenderInfo]:
    name, address = email.utils.parseaddr(from_header)
    if not address or "@" not in address:
        return None

    address = address.strip().lower()
    name = name.strip()
    domain = address.split("@")[1]

    first, last = _split_name(name) if name else _name_from_local(address)
    company = _domain_to_company(domain) if domain not in config.PERSONAL_DOMAINS else ""

    return SenderInfo(
        email=address,
        name=name,
        first_name=first,
        last_name=last,
        domain=domain,
        company=company,
    )


def _split_name(full: str) -> tuple[str, str]:
    parts = full.split(maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")


def _name_from_local(address: str) -> tuple[str, str]:
    local = address.split("@")[0]
    parts = local.replace(".", " ").replace("_", " ").replace("-", " ").split()
    if len(parts) >= 2:
        return parts[0].capitalize(), " ".join(parts[1:]).capitalize()
    return local.capitalize(), ""


def _domain_to_company(domain: str) -> str:
    """'acme-corp.com' → 'Acme Corp', 'sub.bigco.co.uk' → 'Bigco'."""
    parts = domain.split(".")
    # Drop multi-part TLDs (e.g. co.uk, com.br) and single TLDs
    if len(parts) > 2 and len(parts[-2]) <= 3:
        parts = parts[:-2]
    elif len(parts) > 1:
        parts = parts[:-1]
    company_token = parts[-1] if parts else domain
    return company_token.replace("-", " ").replace("_", " ").title()
