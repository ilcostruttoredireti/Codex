"""Gmail API client: autenticazione OAuth2 e lettura email in arrivo."""

import os
import json
import re
import logging
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"


def get_gmail_service():
    """Autentica e restituisce il servizio Gmail."""
    creds = None

    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                raise FileNotFoundError(
                    f"File '{CREDENTIALS_FILE}' non trovato.\n"
                    "Scarica le credenziali OAuth2 da Google Cloud Console:\n"
                    "APIs & Services → Credentials → Create Credentials → OAuth client ID\n"
                    "Tipo: Desktop App. Salva come 'credentials.json'."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def parse_sender(from_header: str) -> dict:
    """
    Estrae nome ed email dall'header From.
    Gestisce formati: 'Nome Cognome <email@domain.com>' oppure 'email@domain.com'
    """
    from_header = from_header.strip()

    # Formato: "Nome <email>"
    match = re.match(r'^"?([^"<]+)"?\s*<([^>]+)>$', from_header)
    if match:
        display_name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        # Solo email
        display_name = ""
        email = from_header.lower()

    parts = display_name.split() if display_name else []
    first_name = parts[0] if parts else ""
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    domain = email.split("@")[1] if "@" in email else ""

    # Ricava nome azienda dal dominio (rimuove TLD e www)
    company = _domain_to_company(domain)

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "display_name": display_name,
        "domain": domain,
        "company": company,
    }


def _domain_to_company(domain: str) -> str:
    """Converte dominio in nome azienda leggibile."""
    if not domain:
        return ""
    # Rimuove TLD comuni e 'www'
    parts = domain.replace("www.", "").split(".")
    # Prende la parte principale (es. 'google' da 'mail.google.com')
    main = parts[-2] if len(parts) >= 2 else parts[0]
    return main.capitalize()


def fetch_new_messages(service, since_history_id: Optional[str] = None, max_results: int = 50) -> list[dict]:
    """
    Recupera i messaggi in arrivo nella INBOX.
    Se since_history_id è fornito, usa Gmail History API per efficienza.
    Altrimenti fa una ricerca dei messaggi più recenti non ancora processati.
    """
    messages = []

    try:
        if since_history_id:
            messages = _fetch_via_history(service, since_history_id)
        else:
            messages = _fetch_recent_inbox(service, max_results)
    except HttpError as e:
        if e.resp.status == 404 and since_history_id:
            # history_id scaduto, ricade su ricerca full
            logger.warning("History ID scaduto, eseguo ricerca completa.")
            messages = _fetch_recent_inbox(service, max_results)
        else:
            raise

    return messages


def _fetch_via_history(service, history_id: str) -> list[dict]:
    """Usa Gmail History API per messaggi aggiunti alla INBOX dall'ultimo sync."""
    results = []
    page_token = None

    while True:
        kwargs = {
            "userId": "me",
            "startHistoryId": history_id,
            "historyTypes": ["messageAdded"],
            "labelId": "INBOX",
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().history().list(**kwargs).execute()
        history_items = response.get("history", [])

        for item in history_items:
            for msg_added in item.get("messagesAdded", []):
                msg = msg_added.get("message", {})
                labels = msg.get("labelIds", [])
                if "INBOX" in labels and "SENT" not in labels:
                    full_msg = _get_message_detail(service, msg["id"])
                    if full_msg:
                        results.append(full_msg)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results


def _fetch_recent_inbox(service, max_results: int) -> list[dict]:
    """Cerca i messaggi recenti nella INBOX."""
    response = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=max_results,
        q="in:inbox -in:sent",
    ).execute()

    messages = []
    for msg_ref in response.get("messages", []):
        full_msg = _get_message_detail(service, msg_ref["id"])
        if full_msg:
            messages.append(full_msg)

    return messages


def _get_message_detail(service, msg_id: str) -> Optional[dict]:
    """Recupera i dettagli di un singolo messaggio."""
    try:
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        if not from_header:
            return None

        sender = parse_sender(from_header)
        if not sender["email"]:
            return None

        return {
            "message_id": msg_id,
            "thread_id": msg.get("threadId", ""),
            "history_id": msg.get("historyId", ""),
            "subject": headers.get("Subject", "(nessun oggetto)"),
            "date": headers.get("Date", ""),
            "sender": sender,
        }
    except HttpError as e:
        logger.error("Errore recupero messaggio %s: %s", msg_id, e)
        return None


def get_profile_history_id(service) -> str:
    """Restituisce l'historyId corrente della mailbox (per il primo avvio)."""
    profile = service.users().getProfile(userId="me").execute()
    return profile.get("historyId", "")
