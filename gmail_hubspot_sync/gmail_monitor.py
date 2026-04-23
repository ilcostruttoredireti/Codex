"""
Gmail monitor: autentica e recupera i nuovi messaggi in arrivo
usando la Gmail API con OAuth2 e il meccanismo history per evitare
di riprocessare email già viste.
"""

import os
import json
import base64
import email as email_lib
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _parse_sender(raw_from: str) -> tuple[str, str]:
    """Ritorna (nome, email) dal campo From:."""
    name, addr = parseaddr(raw_from)
    return name.strip(), addr.strip().lower()


def _extract_domain(email_addr: str) -> str:
    """Estrae il dominio dall'indirizzo email."""
    parts = email_addr.split("@")
    return parts[1] if len(parts) == 2 else ""


def get_gmail_service(credentials_file: str, token_file: str):
    """Autentica e restituisce il servizio Gmail."""
    creds: Optional[Credentials] = None

    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_or_init_history_id(service, history_id_file: str, label: str = "INBOX") -> str:
    """
    Legge l'ultimo historyId salvato su disco.
    Se non esiste, usa l'historyId corrente della mailbox come punto di partenza
    (così al primo avvio non si riprocessano tutte le email vecchie).
    """
    if Path(history_id_file).exists():
        return Path(history_id_file).read_text().strip()

    profile = service.users().getProfile(userId="me").execute()
    current_id = str(profile["historyId"])
    Path(history_id_file).write_text(current_id)
    return current_id


def save_history_id(history_id_file: str, history_id: str) -> None:
    Path(history_id_file).write_text(history_id)


def fetch_new_messages(
    service,
    history_id_file: str,
    label: str = "INBOX",
) -> list[dict]:
    """
    Usa Gmail History API per recuperare solo i messaggi arrivati
    dopo l'ultimo historyId salvato.
    Ritorna lista di dict con: id, subject, sender_name, sender_email, domain.
    """
    start_history_id = get_or_init_history_id(service, history_id_file, label)
    messages_info = []
    new_history_id = start_history_id

    try:
        response = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId=label,
            )
            .execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            # historyId scaduto → reset al corrente
            profile = service.users().getProfile(userId="me").execute()
            new_history_id = str(profile["historyId"])
            save_history_id(history_id_file, new_history_id)
            return []
        raise

    history_records = response.get("history", [])
    if response.get("historyId"):
        new_history_id = str(response["historyId"])

    seen_ids: set[str] = set()
    for record in history_records:
        for added in record.get("messagesAdded", []):
            msg_id = added["message"]["id"]
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            msg_detail = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )

            headers = {
                h["name"]: h["value"]
                for h in msg_detail.get("payload", {}).get("headers", [])
            }
            raw_from = headers.get("From", "")
            subject = headers.get("Subject", "(nessun oggetto)")
            sender_name, sender_email = _parse_sender(raw_from)

            if not sender_email:
                continue

            messages_info.append({
                "id": msg_id,
                "subject": subject,
                "sender_name": sender_name,
                "sender_email": sender_email,
                "domain": _extract_domain(sender_email),
            })

    save_history_id(history_id_file, new_history_id)
    return messages_info
