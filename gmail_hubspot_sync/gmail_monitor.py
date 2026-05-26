"""
Modulo Gmail: autenticazione OAuth2 e lettura dei messaggi in arrivo.

Le dipendenze Google vengono importate in modo lazy per permettere
l'import del modulo anche in ambienti senza le librerie native.
"""
import logging
import os
from dataclasses import dataclass
from typing import List, Optional

from config import GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES, GMAIL_TOKEN_FILE
from parsing_utils import (
    domain_to_company,
    extract_domain,
    parse_from_header,
    parse_name,
)

logger = logging.getLogger(__name__)


@dataclass
class SenderInfo:
    """Dati estratti dal mittente di un'email."""
    message_id: str
    raw_from: str
    email: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    domain: str = ""
    company: str = ""
    subject: str = ""
    date: str = ""


def _authenticate():
    """Autenticazione OAuth2 con refresh automatico del token."""
    # Import lazy delle dipendenze Google (richiedono librerie native)
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Token scaduto — refresh in corso...")
            creds.refresh(Request())
        else:
            if not os.path.exists(GMAIL_CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"File credentials non trovato: {GMAIL_CREDENTIALS_FILE}\n"
                    "Scaricalo da https://console.cloud.google.com/ → "
                    "API & Services → Credentials"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(GMAIL_TOKEN_FILE, "w") as token_file:
            token_file.write(creds.to_json())
        logger.info("Token salvato in %s", GMAIL_TOKEN_FILE)

    return creds


class GmailMonitor:
    def __init__(self):
        self._service = None

    def _get_service(self):
        if self._service is None:
            # Import lazy delle dipendenze Google
            from googleapiclient.discovery import build
            creds = _authenticate()
            self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def fetch_inbox_messages(self, max_results: int = 50) -> List[dict]:
        """
        Recupera i messaggi dalla inbox (esclusi quelli inviati da noi stessi).
        Restituisce una lista di metadati messaggio (id, threadId).
        """
        from googleapiclient.errors import HttpError
        service = self._get_service()
        try:
            results = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    labelIds=["INBOX"],
                    maxResults=max_results,
                    q="-from:me",
                )
                .execute()
            )
            return results.get("messages", [])
        except HttpError as e:
            logger.error("Errore Gmail API (list): %s", e)
            return []

    def get_sender_info(self, message_id: str) -> Optional[SenderInfo]:
        """
        Recupera header del messaggio ed estrae i dati del mittente.
        """
        from googleapiclient.errors import HttpError
        service = self._get_service()
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError as e:
            logger.error("Errore Gmail API (get %s): %s", message_id, e)
            return None

        headers = {h["name"].lower(): h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}

        from_header = headers.get("from", "")
        if not from_header:
            logger.debug("Messaggio %s senza header From — saltato", message_id)
            return None

        display_name, email_addr = parse_from_header(from_header)
        if not email_addr or "@" not in email_addr:
            logger.debug("Email non valida nel messaggio %s: '%s'", message_id, from_header)
            return None

        first_name, last_name = parse_name(display_name)
        domain = extract_domain(email_addr)
        company = domain_to_company(domain)

        return SenderInfo(
            message_id=message_id,
            raw_from=from_header,
            email=email_addr,
            first_name=first_name,
            last_name=last_name,
            full_name=f"{first_name} {last_name}".strip(),
            domain=domain,
            company=company,
            subject=headers.get("subject", ""),
            date=headers.get("date", ""),
        )
