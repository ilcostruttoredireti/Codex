import os
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import (
    GMAIL_SCOPES,
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    FREE_EMAIL_DOMAINS,
    INITIAL_LOOKBACK_HOURS,
)
from .models import SenderInfo

logger = logging.getLogger(__name__)


class GmailClient:
    def __init__(
        self,
        credentials_file: str = GMAIL_CREDENTIALS_FILE,
        token_file: str = GMAIL_TOKEN_FILE,
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None
        self._label_cache: dict[str, str] = {}

    # ──────────────────────────────────────────────
    # Autenticazione
    # ──────────────────────────────────────────────

    def authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing Gmail token...")
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"File credenziali Gmail non trovato: {self.credentials_file}\n"
                        "Scaricalo da Google Cloud Console → API & Servizi → Credenziali."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)

            token_dir = os.path.dirname(self.token_file)
            if token_dir:
                os.makedirs(token_dir, exist_ok=True)
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        self.service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail autenticato con successo.")

    # ──────────────────────────────────────────────
    # Recupero messaggi
    # ──────────────────────────────────────────────

    def get_new_messages(
        self,
        since: Optional[datetime] = None,
        max_results: int = 100,
    ) -> list[dict]:
        if not self.service:
            raise RuntimeError("Chiama authenticate() prima di usare il client.")

        if since is None:
            # Prima esecuzione: lookback configurabile
            since = datetime.now(timezone.utc) - timedelta(hours=INITIAL_LOOKBACK_HOURS)

        # Gmail accetta "after:<epoch_seconds>"
        epoch = int(since.timestamp())
        query = f"in:inbox -from:me after:{epoch}"
        logger.debug(f"Gmail query: {query}")

        try:
            response = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            messages = response.get("messages", [])
            logger.debug(f"Trovati {len(messages)} messaggi da controllare.")
            return messages
        except HttpError as e:
            logger.error(f"Errore Gmail API (list): {e}")
            return []

    def get_message_details(self, message_id: str) -> Optional[dict]:
        try:
            return (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
        except HttpError as e:
            logger.error(f"Errore recupero messaggio {message_id}: {e}")
            return None

    # ──────────────────────────────────────────────
    # Parsing mittente
    # ──────────────────────────────────────────────

    def parse_sender(self, message: dict) -> Optional[SenderInfo]:
        headers = {
            h["name"]: h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "").strip()
        subject = headers.get("Subject", "(nessun oggetto)")
        date_str = headers.get("Date", "")

        if not from_header:
            return None

        name, email_addr = parseaddr(from_header)
        if not email_addr or "@" not in email_addr:
            return None

        email_addr = email_addr.lower().strip()
        domain = email_addr.split("@")[1]

        # Pulizia nome
        name = name.strip().strip('"') or None
        firstname = lastname = None
        if name:
            parts = name.split(None, 1)
            firstname = parts[0] if parts else None
            lastname = parts[1] if len(parts) > 1 else None

        # Azienda dal dominio (solo se non provider gratuito)
        company = None
        if domain not in FREE_EMAIL_DOMAINS:
            base = domain.split(".")[0]
            company = base.capitalize()

        # Data ricezione
        received_at = None
        if date_str:
            try:
                received_at = parsedate_to_datetime(date_str)
            except Exception:
                received_at = datetime.now(timezone.utc)

        return SenderInfo(
            email=email_addr,
            domain=domain,
            raw_from=from_header,
            message_id=message["id"],
            name=name,
            firstname=firstname,
            lastname=lastname,
            company=company,
            subject=subject,
            received_at=received_at,
        )

    # ──────────────────────────────────────────────
    # Gestione etichette
    # ──────────────────────────────────────────────

    def get_or_create_label(self, label_name: str) -> Optional[str]:
        if label_name in self._label_cache:
            return self._label_cache[label_name]
        try:
            result = self.service.users().labels().list(userId="me").execute()
            for lbl in result.get("labels", []):
                if lbl["name"] == label_name:
                    self._label_cache[label_name] = lbl["id"]
                    return lbl["id"]
            # Crea l'etichetta se non esiste
            created = (
                self.service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": label_name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                        "color": {
                            "backgroundColor": "#16a766",
                            "textColor": "#ffffff",
                        },
                    },
                )
                .execute()
            )
            label_id = created["id"]
            self._label_cache[label_name] = label_id
            logger.info(f"Etichetta Gmail creata: '{label_name}' (ID: {label_id})")
            return label_id
        except HttpError as e:
            logger.warning(f"Impossibile gestire etichetta '{label_name}': {e}")
            return None

    def add_label_to_message(self, message_id: str, label_id: str):
        try:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except HttpError as e:
            logger.warning(f"Impossibile applicare etichetta a {message_id}: {e}")
