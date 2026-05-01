#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Polls Gmail inbox for new emails and upserts senders as HubSpot contacts.
"""

import email.utils
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = Path("gmail_token.json")
GMAIL_CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
HUBSPOT_API_KEY = os.environ["HUBSPOT_API_KEY"]
HUBSPOT_BASE_URL = "https://api.hubapi.com"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = Path("sync_state.json")

# Free email domains — do not derive company name from these
_FREE_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "icloud.com", "libero.it", "virgilio.it",
    "live.com", "live.it", "protonmail.com", "tiscali.it",
}

# Automated senders to skip
_SKIP_PATTERNS = re.compile(
    r"(no.?reply|noreply|mailer.daemon|postmaster|do.not.reply|automated|"
    r"bounce|notification|alert|newsletter)@",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("sync.log")],
)
log = logging.getLogger(__name__)


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def build_gmail_service():
    creds: Optional[Credentials] = None
    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_history_id": None, "processed_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_new_message_ids(service, state: dict) -> list[str]:
    """Return IDs of inbox messages received since last run."""
    profile = service.users().getProfile(userId="me").execute()
    current_history_id = profile["historyId"]

    if not state["last_history_id"]:
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=50)
            .execute()
        )
        state["last_history_id"] = current_history_id
        return [m["id"] for m in result.get("messages", [])]

    try:
        history = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=state["last_history_id"],
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            .execute()
        )
    except Exception as exc:
        if "404" in str(exc) or "Invalid" in str(exc):
            log.warning("History ID scaduto, ricarico messaggi recenti")
            state["last_history_id"] = None
            return fetch_new_message_ids(service, state)
        raise

    state["last_history_id"] = current_history_id
    ids = []
    for record in history.get("history", []):
        for added in record.get("messagesAdded", []):
            msg = added["message"]
            if "INBOX" in msg.get("labelIds", []):
                ids.append(msg["id"])
    return ids


def extract_sender(service, message_id: str) -> Optional[dict]:
    """Return sender metadata from a Gmail message."""
    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    from_header = headers.get("From", "")
    display_name, addr = email.utils.parseaddr(from_header)
    addr = addr.lower().strip()
    if not addr or "@" not in addr:
        return None
    return {
        "email": addr,
        "display_name": display_name.strip() or None,
        "domain": addr.split("@")[1],
        "subject": headers.get("Subject", "(no subject)"),
        "date": headers.get("Date", ""),
        "message_id": message_id,
    }


# ── Name / company parsing ─────────────────────────────────────────────────────

def parse_name(display_name: Optional[str], addr: str) -> tuple[str, str]:
    """Return (firstname, lastname). Falls back to email local part."""
    if display_name:
        parts = display_name.split()
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        return parts[0], ""
    local = re.sub(r"[._\-+]", " ", addr.split("@")[0]).strip()
    parts = local.split()
    if len(parts) >= 2:
        return parts[0].capitalize(), " ".join(p.capitalize() for p in parts[1:])
    return local.capitalize(), ""


def domain_to_company(domain: str) -> str:
    if domain in _FREE_DOMAINS:
        return ""
    name = domain.split(".")[0]
    return name.replace("-", " ").title()


# ── HubSpot API ────────────────────────────────────────────────────────────────

_HS_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_API_KEY}",
    "Content-Type": "application/json",
}


def _hs_request(method: str, path: str, **kwargs) -> dict:
    resp = httpx.request(
        method,
        f"{HUBSPOT_BASE_URL}{path}",
        headers=_HS_HEADERS,
        timeout=15,
        **kwargs,
    )
    resp.raise_for_status()
    return resp.json()


def find_contact(addr: str) -> Optional[dict]:
    data = _hs_request(
        "POST",
        "/crm/v3/objects/contacts/search",
        json={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": addr}]}
            ],
            "properties": ["email", "firstname", "lastname", "company"],
            "limit": 1,
        },
    )
    results = data.get("results", [])
    return results[0] if results else None


def create_contact(props: dict) -> dict:
    return _hs_request("POST", "/crm/v3/objects/contacts", json={"properties": props})


def update_contact(contact_id: str, props: dict) -> dict:
    return _hs_request(
        "PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": props}
    )


def add_note(contact_id: str, body: str) -> None:
    note = _hs_request(
        "POST",
        "/crm/v3/objects/notes",
        json={
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            }
        },
    )
    note_id = note["id"]
    _hs_request(
        "PUT",
        f"/crm/v4/objects/notes/{note_id}/associations/contacts/{contact_id}",
        json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
    )


# ── Sync core ──────────────────────────────────────────────────────────────────

def sync_sender(sender: dict) -> dict:
    """Upsert a single sender into HubSpot. Returns a result dict."""
    addr = sender["email"]
    firstname, lastname = parse_name(sender["display_name"], addr)
    company = domain_to_company(sender["domain"])

    existing = find_contact(addr)

    note_body = (
        f"📧 Email ricevuta via Gmail\n"
        f"Oggetto: {sender['subject']}\n"
        f"Data: {sender['date']}\n"
        f"Tag: Inbound Gmail"
    )

    if existing:
        contact_id = existing["id"]
        ep = existing.get("properties", {})
        updates = {}
        if firstname and not ep.get("firstname"):
            updates["firstname"] = firstname
        if lastname and not ep.get("lastname"):
            updates["lastname"] = lastname
        if company and not ep.get("company"):
            updates["company"] = company

        if updates:
            update_contact(contact_id, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"

        try:
            add_note(contact_id, note_body)
        except Exception as exc:
            log.warning("Nota non aggiunta (%s): %s", addr, exc)

        return {"status": status, "email": addr, "hubspot_id": contact_id}

    props: dict = {"email": addr, "firstname": firstname}
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    new = create_contact(props)
    contact_id = new["id"]

    try:
        add_note(contact_id, note_body.replace("ricevuta", "primo contatto"))
    except Exception as exc:
        log.warning("Nota non aggiunta (%s): %s", addr, exc)

    return {"status": "Creato", "email": addr, "hubspot_id": contact_id}


# ── Main loop ──────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Avvio Gmail → HubSpot sync | polling ogni %ds", POLL_INTERVAL)
    service = build_gmail_service()
    my_email = (
        service.users().getProfile(userId="me").execute()["emailAddress"].lower()
    )
    log.info("Account Gmail: %s", my_email)

    state = load_state()

    while True:
        try:
            message_ids = fetch_new_message_ids(service, state)
            save_state(state)

            processed = state.setdefault("processed_ids", [])
            seen_in_batch: set[str] = set()
            batch_results: list[dict] = []

            for mid in message_ids:
                if mid in processed:
                    continue

                sender = extract_sender(service, mid)
                if not sender:
                    continue

                addr = sender["email"]
                if addr == my_email or addr in seen_in_batch:
                    continue
                if _SKIP_PATTERNS.search(addr):
                    log.debug("Skip automatico: %s", addr)
                    continue

                seen_in_batch.add(addr)
                result = sync_sender(sender)
                batch_results.append(result)
                log.info("[%s] %s → HubSpot ID: %s", result["status"], addr, result["hubspot_id"])

                processed.append(mid)
                if len(processed) > 1000:
                    state["processed_ids"] = processed[-1000:]
                save_state(state)

            if batch_results:
                _print_results(batch_results)

        except Exception as exc:
            log.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


def _print_results(results: list[dict]) -> None:
    print("\n┌─ Risultati sincronizzazione ─────────────────────────────────────────┐")
    header = f"│ {'Stato':<12} │ {'Email':<38} │ {'HubSpot ID':<14} │"
    print(header)
    print("├" + "─" * 14 + "┼" + "─" * 40 + "┼" + "─" * 16 + "┤")
    for r in results:
        status_icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️"}.get(r["status"], "")
        print(f"│ {status_icon} {r['status']:<10} │ {r['email']:<38} │ {str(r['hubspot_id']):<14} │")
    print("└" + "─" * 14 + "┴" + "─" * 40 + "┴" + "─" * 16 + "┘")


if __name__ == "__main__":
    main()
