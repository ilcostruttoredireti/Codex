"""
Gestisce l'autenticazione Gmail e il recupero delle email in arrivo
tramite l'API Gmail (History API per polling efficiente).
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    full_name: str
    domain: str
    company: str  # ricavato dal dominio, es. "acme.com" → "Acme"
    raw_from: str


def _parse_from_header(from_header: str) -> SenderInfo:
    """
    Analizza l'header From di un'email.
    Formati supportati:
      - "Nome Cognome <email@domain.com>"
      - "email@domain.com"
    """
    from_header = from_header.strip()
    email = ""
    full_name = ""

    # Cerca pattern "Nome <email>"
    match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>', from_header)
    if match:
        full_name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        # Solo indirizzo email
        email = from_header.lower()

    if not email:
        email = from_header.lower()

    # Estrai dominio
    domain = email.split("@")[-1] if "@" in email else ""

    # Azienda dal dominio (rimuovi TLD comuni e capitalizza)
    company = _domain_to_company(domain)

    # Separa nome e cognome
    parts = full_name.split() if full_name else []
    first_name = parts[0] if parts else ""
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    return SenderInfo(
        email=email,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        domain=domain,
        company=company,
        raw_from=from_header,
    )


def _domain_to_company(domain: str) -> str:
    """
    Converte un dominio in un nome azienda plausibile.
    Es: "acme-corp.com" → "Acme Corp"
    """
    if not domain:
        return ""
    # Rimuovi TLD
    parts = domain.split(".")
    if len(parts) >= 2:
        name = parts[-2]  # es. "acme" da "acme.com"
    else:
        name = parts[0]

    # Ignora nomi generici
    generic = {"gmail", "yahoo", "hotmail", "outlook", "icloud", "protonmail", "live"}
    if name.lower() in generic:
        return ""

    # Capitalizza e sostituisci trattini/underscore con spazi
    return name.replace("-", " ").replace("_", " ").title()


def get_gmail_service():
    """Autentica e restituisce il servizio Gmail API."""
    creds: Optional[Credentials] = None

    token_path = Path(config.GOOGLE_TOKEN_FILE)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), config.GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(config.GOOGLE_CREDENTIALS_FILE).exists():
                raise FileNotFoundError(
                    f"File credenziali Google non trovato: {config.GOOGLE_CREDENTIALS_FILE}\n"
                    "Scaricalo da Google Cloud Console e salvalo come 'credentials.json'."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GOOGLE_CREDENTIALS_FILE, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_profile_history_id(service) -> str:
    """Restituisce il historyId attuale della casella (punto di partenza)."""
    profile = service.users().getProfile(userId="me").execute()
    return profile["historyId"]


def get_new_messages(service, start_history_id: str) -> tuple[list[SenderInfo], str]:
    """
    Recupera i messaggi arrivati dopo start_history_id usando History API.
    Restituisce (lista_mittenti, nuovo_history_id).
    """
    senders: list[SenderInfo] = []
    new_history_id = start_history_id

    try:
        response = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                labelId=config.GMAIL_LABEL,
                historyTypes=["messageAdded"],
            )
            .execute()
        )
    except Exception as e:
        # historyId scaduto (>7 giorni) → resetta al corrente
        if "404" in str(e) or "invalid" in str(e).lower():
            new_history_id = get_profile_history_id(service)
            return senders, new_history_id
        raise

    if "historyId" in response:
        new_history_id = response["historyId"]

    histories = response.get("history", [])
    processed_ids: set[str] = set()

    for record in histories:
        for msg_added in record.get("messagesAdded", []):
            msg = msg_added.get("message", {})
            msg_id = msg.get("id")
            if not msg_id or msg_id in processed_ids:
                continue
            processed_ids.add(msg_id)

            # Recupera solo gli header necessari
            detail = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata", metadataHeaders=["From"])
                .execute()
            )

            from_header = _extract_from_header(detail)
            if not from_header:
                continue

            sender = _parse_from_header(from_header)

            # Filtra mittenti da ignorare
            if _should_ignore(sender):
                continue

            senders.append(sender)

    return senders, new_history_id


def _extract_from_header(message_detail: dict) -> str:
    """Estrae il valore dell'header From dai metadati del messaggio."""
    headers = message_detail.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == "from":
            return h.get("value", "")
    return ""


def _should_ignore(sender: SenderInfo) -> bool:
    """Verifica se il mittente va ignorato (noreply, bounce, ecc.)."""
    email_lower = sender.email.lower()

    if email_lower in config.IGNORED_EMAILS:
        return True

    if sender.domain in config.IGNORED_DOMAINS:
        return True

    # Ignora indirizzi noreply/no-reply/donotreply
    local = email_lower.split("@")[0]
    if any(kw in local for kw in ("noreply", "no-reply", "donotreply", "bounce", "mailer-daemon")):
        return True

    return False
