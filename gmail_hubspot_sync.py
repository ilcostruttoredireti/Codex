#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and upserts senders as HubSpot contacts.

Output per email processed:
  [Creato | Aggiornato | Ignorato]  <email>  →  HubSpot ID: <id>
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import ApiException
from hubspot.crm.contacts.models import (
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.objects.models import SimplePublicObjectInputForCreate as NoteCreate

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", ".sync_state.json")
MAX_PROCESSED_IDS = 10_000  # rolling cap to bound memory

# Domains that should not produce a company name
_FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "icloud.com",
    "me.com", "protonmail.com", "proton.me", "aol.com", "mail.com",
    "gmx.com", "gmx.net", "zoho.com", "fastmail.com", "tutanota.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it", "tin.it",
}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    p = Path(STATE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_history_id": None, "processed_ids": []}


def _save_state(state: dict) -> None:
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))


# ── Gmail ─────────────────────────────────────────────────────────────────────

def _get_gmail_service():
    creds: Optional[Credentials] = None
    token_path = Path(GMAIL_TOKEN_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _fetch_new_message_stubs(service, state: dict) -> list[dict]:
    """Return minimal message stubs ({id, threadId}) for emails not yet processed."""
    history_id = state.get("last_history_id")

    if history_id:
        try:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            messages = [
                msg["message"]
                for record in resp.get("history", [])
                for msg in record.get("messagesAdded", [])
            ]
            if "historyId" in resp:
                state["last_history_id"] = resp["historyId"]
            return messages
        except HttpError as exc:
            if exc.resp.status == 404:
                log.warning("History ID expired — reseeding from inbox listing.")
                state["last_history_id"] = None
            else:
                raise

    # First run or expired history: list recent INBOX messages to seed state.
    resp = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=50)
        .execute()
    )
    messages = resp.get("messages", [])

    profile = service.users().getProfile(userId="me").execute()
    state["last_history_id"] = profile["historyId"]

    if not state["processed_ids"]:
        # Mark all pre-existing messages as already processed.
        state["processed_ids"] = [m["id"] for m in messages]
        _save_state(state)
        log.info(
            "Prima esecuzione: %d messaggi pre-esistenti ignorati, ora in ascolto.",
            len(messages),
        )
        return []

    return messages


def _parse_sender(headers: list[dict]) -> Optional[tuple[str, str, str]]:
    """
    Parse the From header.
    Returns (email_addr, full_name, domain) or None.
    """
    header_map = {h["name"].lower(): h["value"] for h in headers}
    raw_from = header_map.get("from", "").strip()
    if not raw_from:
        return None

    full_name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.strip().lower()
    if not email_addr or "@" not in email_addr:
        return None

    domain = email_addr.split("@", 1)[1]
    return email_addr, full_name.strip(), domain


# ── HubSpot ───────────────────────────────────────────────────────────────────

def _hs_client():
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def _find_contact(hs, email: str) -> Optional[object]:
    """Return the first matching HubSpot contact or None."""
    try:
        result = hs.crm.contacts.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
                limit=1,
            )
        )
        return result.results[0] if result.total > 0 else None
    except ApiException as exc:
        log.error("HubSpot search failed for %s: %s", email, exc)
        return None


def _split_name(full_name: str) -> tuple[str, str]:
    """'Mario Rossi' → ('Mario', 'Rossi').  Handles single-word names."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _company_from_domain(domain: str) -> str:
    """Return a best-effort company name from the email domain."""
    if domain in _FREE_EMAIL_DOMAINS:
        return ""
    return domain.split(".")[0].capitalize()


def _upsert_contact(
    hs, email: str, full_name: str, domain: str
) -> tuple[str, str]:
    """
    Create or update a HubSpot contact.
    Returns (stato, contact_id) where stato ∈ {'Creato', 'Aggiornato', 'Ignorato'}.
    """
    firstname, lastname = _split_name(full_name) if full_name else ("", "")
    company = _company_from_domain(domain)
    existing = _find_contact(hs, email)

    if existing:
        cid = existing.id
        props = existing.properties or {}
        updates: dict[str, str] = {}

        if firstname and not props.get("firstname"):
            updates["firstname"] = firstname
        if lastname and not props.get("lastname"):
            updates["lastname"] = lastname
        if company and not props.get("company"):
            updates["company"] = company
        if not props.get("hs_lead_source"):
            updates["hs_lead_source"] = "Gmail"

        if updates:
            try:
                hs.crm.contacts.basic_api.update(
                    contact_id=cid,
                    simple_public_object_input=SimplePublicObjectInput(properties=updates),
                )
                log.info("Aggiornato contatto %s (id=%s)", email, cid)
                return "Aggiornato", cid
            except ApiException as exc:
                log.error("HubSpot update error (%s): %s", email, exc)
                return "Ignorato", cid
        else:
            log.info("Nessuna modifica necessaria per %s (id=%s)", email, cid)
            return "Ignorato", cid
    else:
        new_props: dict[str, str] = {
            "email": email,
            "hs_lead_source": "Gmail",
        }
        if firstname:
            new_props["firstname"] = firstname
        if lastname:
            new_props["lastname"] = lastname
        if company:
            new_props["company"] = company

        try:
            contact = hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=new_props,
                    associations=[],
                )
            )
            cid = contact.id
            log.info("Creato contatto %s (id=%s)", email, cid)
            return "Creato", cid
        except ApiException as exc:
            log.error("HubSpot create error (%s): %s", email, exc)
            return "Ignorato", "N/A"


def _add_inbound_note(hs, contact_id: str, email_addr: str) -> None:
    """Attach a timeline note to the contact recording the inbound Gmail event."""
    timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    try:
        hs.crm.objects.basic_api.create(
            object_type="notes",
            simple_public_object_input_for_create=NoteCreate(
                properties={
                    "hs_note_body": (
                        f"Email inbound ricevuta da {email_addr} tramite Gmail.\n"
                        "Tag: Inbound Gmail"
                    ),
                    "hs_timestamp": timestamp_ms,
                },
                associations=[
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": 202,  # Note → Contact
                            }
                        ],
                    }
                ],
            ),
        )
        log.debug("Nota timeline aggiunta per contatto %s", contact_id)
    except Exception as exc:
        log.debug("Impossibile aggiungere nota timeline: %s", exc)


# ── Core processing loop ──────────────────────────────────────────────────────

def _process_once(gmail, hs, state: dict) -> list[dict]:
    results = []
    stubs = _fetch_new_message_stubs(gmail, state)

    for stub in stubs:
        msg_id = stub["id"]
        if msg_id in state["processed_ids"]:
            continue

        try:
            detail = (
                gmail.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
            )
        except HttpError as exc:
            log.warning("Impossibile recuperare messaggio %s: %s", msg_id, exc)
            state["processed_ids"].append(msg_id)
            continue

        headers = detail.get("payload", {}).get("headers", [])
        sender = _parse_sender(headers)

        if not sender:
            state["processed_ids"].append(msg_id)
            continue

        email_addr, full_name, domain = sender
        log.info("Elaborazione email da %s (%s)", email_addr, full_name or "—")

        stato, cid = _upsert_contact(hs, email_addr, full_name, domain)

        if stato == "Creato":
            _add_inbound_note(hs, cid, email_addr)

        results.append({"stato": stato, "email": email_addr, "hubspot_id": cid})
        state["processed_ids"].append(msg_id)

        # Keep list bounded
        if len(state["processed_ids"]) > MAX_PROCESSED_IDS:
            state["processed_ids"] = state["processed_ids"][-MAX_PROCESSED_IDS:]

    _save_state(state)
    return results


def _print_result(r: dict) -> None:
    stato_pad = r["stato"].ljust(10)
    print(f"  [{stato_pad}]  Email: {r['email']}  |  HubSpot ID: {r['hubspot_id']}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not HUBSPOT_ACCESS_TOKEN:
        raise SystemExit(
            "Errore: HUBSPOT_ACCESS_TOKEN non impostato. "
            "Copia .env.example in .env e compila le variabili."
        )
    if not Path(GMAIL_CREDENTIALS_FILE).exists():
        raise SystemExit(
            f"Errore: file credenziali Gmail '{GMAIL_CREDENTIALS_FILE}' non trovato. "
            "Scarica il file OAuth 2.0 dalla Google Cloud Console."
        )

    gmail = _get_gmail_service()
    hs = _hs_client()
    state = _load_state()

    log.info(
        "Sync Gmail → HubSpot avviato (polling ogni %ds). Premi Ctrl+C per fermare.",
        POLL_INTERVAL,
    )

    while True:
        try:
            results = _process_once(gmail, hs, state)
            if results:
                print(f"\n── {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                      f"── {len(results)} email elaborate ──")
                for r in results:
                    _print_result(r)
                print()
            else:
                log.debug("Nessuna nuova email da elaborare.")
        except HttpError as exc:
            log.error("Errore Gmail API: %s", exc)
        except Exception as exc:
            log.error("Errore imprevisto: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
