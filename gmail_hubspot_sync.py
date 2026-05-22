#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox continuously, extracts sender contacts,
and creates/updates them in HubSpot avoiding duplicates.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
# Your own address — never synced as a contact
OWN_EMAIL = os.getenv("OWN_EMAIL", "").lower()

# Senders/domains that produce no useful contact (notifications, bots, etc.)
SKIP_LOCAL_PARTS = frozenset({
    "noreply", "no-reply", "donotreply", "notifications",
    "notification", "mailer-daemon", "postmaster", "bounce",
    "admin", "support", "info",
})
SKIP_DOMAINS = frozenset({
    "facebookmail.com", "twitter.com", "linkedin.com",
    "bounce.linkedin.com", "accounts.google.com",
    "googlemail.com", "email.amazon.com",
})
# Free email providers — company name not derived from domain
FREE_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com",
    "outlook.com", "live.com", "icloud.com", "me.com",
    "libero.it", "virgilio.it", "tin.it", "tiscali.it",
    "alice.it", "fastwebnet.it",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────────────────────


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_history_id": None, "processed_ids": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Gmail ─────────────────────────────────────────────────────────────────────


def get_gmail_service():
    # Imported here so the script can be imported without google-auth installed
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _get_header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def fetch_new_messages(service, last_history_id: str | None) -> list[dict]:
    """Return inbox messages not yet seen, newest last."""
    messages = []

    if last_history_id:
        try:
            history = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=last_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            for record in history.get("history", []):
                for item in record.get("messagesAdded", []):
                    msg = (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=item["message"]["id"],
                            format="metadata",
                            metadataHeaders=["From", "Subject"],
                        )
                        .execute()
                    )
                    messages.append(msg)
            return messages
        except Exception as exc:
            log.warning("History fetch failed (%s) — falling back to list", exc)

    # First run or history unavailable: fetch recent inbox
    result = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=50)
        .execute()
    )
    for item in result.get("messages", []):
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=item["id"],
                format="metadata",
                metadataHeaders=["From", "Subject"],
            )
            .execute()
        )
        messages.append(msg)
    return messages


# ── Sender parsing ────────────────────────────────────────────────────────────

# Matches "Name <email>" or plain email inside a forwarded header line
_FWD_PATTERN = re.compile(
    r"(?:Da|From)\s*[:\"]?\s*(?:\"?([^\"<\n]+)\"?\s*)?<([^>]+@[^>]+)>",
    re.IGNORECASE,
)


def _extract_original_sender(snippet: str) -> tuple[str, str] | None:
    """Try to pull the original sender name/email from a forwarded snippet."""
    m = _FWD_PATTERN.search(snippet)
    if m:
        return m.group(1) or "", m.group(2).strip()
    return None


def parse_sender(from_header: str, snippet: str = "") -> dict | None:
    """
    Returns a dict with email, first_name, last_name, company, domain,
    or None if the sender should be skipped.
    """
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()

    if not email or "@" not in email:
        return None

    local, domain = email.split("@", 1)

    # Skip own email
    if email == OWN_EMAIL:
        return None

    # Skip notification/bot senders
    if domain in SKIP_DOMAINS:
        return None
    if local in SKIP_LOCAL_PARTS:
        return None

    # For well-known forwarding addresses try to get the real sender
    if snippet and (
        local in {"redazione", "noreply"} or "fw:" in display_name.lower()
    ):
        orig = _extract_original_sender(snippet)
        if orig:
            display_name, email = orig
            local, domain = email.lower().split("@", 1) if "@" in email else (email, "")

    # Split display name into first / last
    parts = display_name.strip().split(None, 1) if display_name.strip() else []
    first_name = parts[0].capitalize() if parts else ""
    last_name = parts[1].capitalize() if len(parts) > 1 else ""

    # Derive company from domain when not a free provider
    company = ""
    if domain and domain not in FREE_DOMAINS:
        company = domain.split(".")[0].capitalize()

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "domain": domain,
    }


# ── HubSpot ───────────────────────────────────────────────────────────────────

HUBSPOT_BASE = "https://api.hubapi.com"


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email: str) -> dict | None:
    body = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company"],
        "limit": 1,
    }
    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json=body,
        timeout=10,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(sender: dict) -> dict:
    props = {
        "email": sender["email"],
        "hs_lead_status": "NEW",
        "leadsource": "OTHER",
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]

    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, existing_props: dict, sender: dict) -> bool:
    """Update only fields that are currently empty. Returns True if any update was made."""
    updates: dict[str, str] = {}

    if not existing_props.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not existing_props.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not existing_props.get("company") and sender["company"]:
        updates["company"] = sender["company"]

    if not updates:
        return False

    r = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
        timeout=10,
    )
    r.raise_for_status()
    return True


def hs_add_activity_note(contact_id: str, email_addr: str, subject: str) -> None:
    """Attach an email-received note to the contact's timeline."""
    body = (
        f"📬 Email ricevuta via Gmail\n"
        f"Da: {email_addr}\n"
        f"Oggetto: {subject}\n"
        f"Fonte: Gmail | Tag: Inbound Gmail"
    )
    props = {
        "hs_note_body": body,
        "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
    }
    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/notes",
        headers=_hs_headers(),
        json={
            "properties": props,
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,  # note → contact
                        }
                    ],
                }
            ],
        },
        timeout=10,
    )
    if r.status_code not in (200, 201):
        log.warning("Note creation failed for contact %s: %s", contact_id, r.text)


# ── Sync one message ──────────────────────────────────────────────────────────


def sync_message(message: dict, state: dict) -> dict:
    msg_id = message["id"]
    from_header = _get_header(message, "from")
    subject = _get_header(message, "subject")
    snippet = message.get("snippet", "")

    if not from_header:
        return {"status": "Ignorato", "motivo": "nessun header From", "msg_id": msg_id}

    sender = parse_sender(from_header, snippet)
    if not sender:
        return {"status": "Ignorato", "motivo": "mittente non valido/da saltare", "msg_id": msg_id}

    existing = hs_find_contact(sender["email"])

    if existing:
        contact_id = existing["id"]
        updated = hs_update_contact(contact_id, existing.get("properties", {}), sender)
        status = "Aggiornato" if updated else "Ignorato"
    else:
        result = hs_create_contact(sender)
        contact_id = result["id"]
        status = "Creato"

    hs_add_activity_note(contact_id, sender["email"], subject)

    # Advance history cursor
    if message.get("historyId"):
        state["last_history_id"] = message["historyId"]
    state.setdefault("processed_ids", []).append(msg_id)
    state["processed_ids"] = state["processed_ids"][-2000:]

    return {
        "status": status,
        "email": sender["email"],
        "hubspot_id": contact_id,
    }


# ── Main loop ─────────────────────────────────────────────────────────────────


def main() -> None:
    if not HUBSPOT_ACCESS_TOKEN:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN non configurato nel file .env")
    if not Path(GMAIL_CREDENTIALS_FILE).exists():
        raise SystemExit(f"File credenziali Gmail non trovato: {GMAIL_CREDENTIALS_FILE}")

    log.info("▶ Gmail → HubSpot Sync avviato (polling ogni %ds)", POLL_INTERVAL)
    service = get_gmail_service()
    state = load_state()

    while True:
        try:
            messages = fetch_new_messages(service, state.get("last_history_id"))
            processed_ids = set(state.get("processed_ids", []))
            new_msgs = [m for m in messages if m["id"] not in processed_ids]

            if new_msgs:
                log.info("📧 %d nuove email da processare", len(new_msgs))
                for msg in new_msgs:
                    result = sync_message(msg, state)
                    save_state(state)
                    status = result["status"]
                    email = result.get("email", "—")
                    hs_id = result.get("hubspot_id", "—")
                    motivo = result.get("motivo", "")
                    if motivo:
                        log.info("[%s] %s (%s)", status, email, motivo)
                    else:
                        log.info("[%s] email=%s  hubspot_id=%s", status, email, hs_id)
            else:
                log.debug("Nessuna nuova email.")

        except KeyboardInterrupt:
            log.info("Sync interrotto dall'utente.")
            break
        except Exception as exc:
            log.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
