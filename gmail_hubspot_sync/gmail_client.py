"""Client Gmail: autenticazione OAuth2 e lettura email in arrivo."""

from __future__ import annotations

import email as email_lib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import config

log = logging.getLogger(__name__)


@dataclass
class SenderInfo:
    """Dati estratti dall'header 'From' di un'email."""

    raw_from: str
    email: str
    display_name: str = ""
    first_name: str = ""
    last_name: str = ""
    domain: str = ""
    message_id: str = ""
    subject: str = ""

    def __post_init__(self) -> None:
        if not self.domain and "@" in self.email:
            self.domain = self.email.split("@", 1)[1].lower()


def _parse_name(display_name: str) -> tuple[str, str]:
    """
    Prova a dividere un nome visualizzato in (first_name, last_name).
    Gestisce:  "Mario Rossi", "Rossi, Mario", "Mario"
    """
    display_name = display_name.strip().strip('"').strip("'")
    if not display_name:
        return "", ""

    # "Cognome, Nome"
    if "," in display_name:
        parts = [p.strip() for p in display_name.split(",", 1)]
        return parts[1], parts[0]

    parts = display_name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _parse_from_header(raw_from: str) -> tuple[str, str]:
    """
    Estrae (display_name, email_address) dall'header From.
    Gestisce formati:
      - "Mario Rossi <mario@example.com>"
      - "<mario@example.com>"
      - "mario@example.com"
    """
    # Usa il parser stdlib per decodificare RFC 2047 (es. =?UTF-8?...)
    name, addr = email_lib.utils.parseaddr(raw_from)
    return name.strip(), addr.strip().lower()


class GmailClient:
    """
    Wrapper intorno all'API Gmail v1.

    Autenticazione: OAuth2 con refresh automatico del token.
    Strategia di monitoraggio: Gmail History API (incrementale, efficiente).
    """

    def __init__(self) -> None:
        self._service = None
        self._creds: Credentials | None = None
        self._state_path = Path(config.STATE_FILE)
        self._state: dict = self._load_state()

    # ── Autenticazione ──────────────────────────────────────────────────────

    def _authenticate(self) -> None:
        """
        Carica o rinnova le credenziali OAuth2.
        Al primo avvio apre il browser per il consenso dell'utente.
        """
        token_path = Path(config.GMAIL_TOKEN_FILE)
        creds: Credentials | None = None

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(token_path), config.GMAIL_SCOPES
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                log.info("Rinnovo token Gmail scaduto…")
                creds.refresh(Request())
            else:
                credentials_path = Path(config.GMAIL_CREDENTIALS_FILE)
                if not credentials_path.exists():
                    raise FileNotFoundError(
                        f"File credenziali Gmail non trovato: {credentials_path}\n"
                        "Scaricalo da Google Cloud Console e salvalo come 'credentials.json'."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path), config.GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)

            token_path.write_text(creds.to_json())
            log.info("Token Gmail salvato in %s", token_path)

        self._creds = creds
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        log.info("Autenticazione Gmail completata.")

    @property
    def service(self):
        """Restituisce il servizio Gmail autenticato (lazy init)."""
        if self._service is None:
            self._authenticate()
        return self._service

    # ── State (historyId) ───────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("State file corrotto, ricreo da zero.")
        return {}

    def _save_state(self) -> None:
        self._state_path.write_text(json.dumps(self._state, indent=2))

    @property
    def last_history_id(self) -> str | None:
        return self._state.get("last_history_id")

    @last_history_id.setter
    def last_history_id(self, value: str) -> None:
        self._state["last_history_id"] = value
        self._save_state()

    # ── Recupero email ──────────────────────────────────────────────────────

    def _get_current_history_id(self) -> str:
        """Recupera l'historyId corrente della casella (per la prima esecuzione)."""
        profile = (
            self.service.users()
            .getProfile(userId=config.GMAIL_USER_ID)
            .execute()
        )
        return profile["historyId"]

    def _fetch_sender_from_message(self, message_id: str) -> SenderInfo | None:
        """
        Scarica l'header From di un singolo messaggio e restituisce SenderInfo.
        Usa format=metadata per scaricare solo gli header (efficiente).
        """
        try:
            msg = (
                self.service.users()
                .messages()
                .get(
                    userId=config.GMAIL_USER_ID,
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
            )
        except HttpError as exc:
            log.warning("Impossibile recuperare messaggio %s: %s", message_id, exc)
            return None

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        raw_from = headers.get("from", "")
        subject = headers.get("subject", "")

        if not raw_from:
            return None

        display_name, email_addr = _parse_from_header(raw_from)
        if not email_addr or "@" not in email_addr:
            log.debug("Header From non valido per msg %s: %s", message_id, raw_from)
            return None

        first_name, last_name = _parse_name(display_name)
        return SenderInfo(
            raw_from=raw_from,
            email=email_addr,
            display_name=display_name,
            first_name=first_name,
            last_name=last_name,
            message_id=message_id,
            subject=subject,
        )

    def _should_ignore(self, sender: SenderInfo, own_email: str | None) -> bool:
        """Ritorna True se il mittente va ignorato."""
        domain = sender.domain.lower()

        if domain in config.ALL_IGNORE_DOMAINS:
            log.debug("Dominio ignorato: %s", domain)
            return True

        # Ignora noreply, no-reply, donotreply, mailer-daemon
        local = sender.email.split("@")[0].lower()
        if any(kw in local for kw in ("noreply", "no-reply", "donotreply", "mailer-daemon", "bounce")):
            log.debug("Indirizzo noreply ignorato: %s", sender.email)
            return True

        if config.IGNORE_SELF and own_email and sender.email == own_email.lower():
            log.debug("Email propria ignorata: %s", sender.email)
            return True

        return False

    def get_new_senders(self) -> Iterator[SenderInfo]:
        """
        Generatore: restituisce SenderInfo per ogni mittente di nuove email.

        Alla prima esecuzione inizializza l'historyId e non restituisce nulla
        (baseline): le email successive saranno processate nelle chiamate successive.
        """
        # Prima esecuzione: imposta baseline
        if self.last_history_id is None:
            history_id = self._get_current_history_id()
            self.last_history_id = history_id
            log.info(
                "Prima esecuzione: baseline historyId=%s. "
                "Le prossime email in arrivo saranno processate.",
                history_id,
            )
            return

        # Recupera il profilo per sapere l'email propria
        try:
            profile = (
                self.service.users()
                .getProfile(userId=config.GMAIL_USER_ID)
                .execute()
            )
            own_email: str | None = profile.get("emailAddress", "").lower() or None
        except HttpError:
            own_email = None

        # Usa History API per ottenere i cambiamenti
        page_token: str | None = None
        new_history_id: str = self.last_history_id
        seen_message_ids: set[str] = set()

        while True:
            try:
                kwargs: dict = {
                    "userId": config.GMAIL_USER_ID,
                    "startHistoryId": self.last_history_id,
                    "historyTypes": ["messageAdded"],
                    "labelId": "INBOX",
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                result = self.service.users().history().list(**kwargs).execute()
            except HttpError as exc:
                if exc.resp.status == 404:
                    # historyId troppo vecchio: reimposta baseline
                    log.warning(
                        "historyId scaduto (404). Reimposto baseline."
                    )
                    self.last_history_id = self._get_current_history_id()
                    return
                log.error("Errore History API: %s", exc)
                return

            # Aggiorna il nuovo historyId dalla risposta
            if "historyId" in result:
                new_history_id = result["historyId"]

            for history_record in result.get("history", []):
                for added in history_record.get("messagesAdded", []):
                    msg_id = added["message"]["id"]
                    if msg_id in seen_message_ids:
                        continue
                    seen_message_ids.add(msg_id)

                    sender = self._fetch_sender_from_message(msg_id)
                    if sender is None:
                        continue

                    if self._should_ignore(sender, own_email):
                        log.debug("Mittente ignorato: %s", sender.email)
                        continue

                    yield sender

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        # Salva il nuovo historyId solo dopo aver processato tutto
        self.last_history_id = new_history_id
        log.debug("Nuovo historyId salvato: %s", new_history_id)
