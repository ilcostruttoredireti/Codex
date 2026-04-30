#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors incoming Gmail messages and syncs sender contacts to HubSpot.
Tracks state via a local JSON file; uses Gmail history API for efficient polling.

Required env vars:
  HUBSPOT_API_TOKEN      HubSpot private app token
  MY_EMAIL               Your own email address (messages from self are skipped)

Optional env vars:
  GMAIL_CREDENTIALS_FILE Path to OAuth credentials JSON  (default: credentials.json)
  GMAIL_TOKEN_FILE       Path to cached OAuth token JSON  (default: token.json)
  STATE_FILE             Path to polling state file        (default: .sync_state.json)
  POLL_INTERVAL_SECONDS  Seconds between polls             (default: 60)
  ENABLE_NOTES           Create HubSpot notes per email   (default: true)
  LEAD_SOURCE_PROPERTY   HubSpot property for source tag  (default: lead_source)
"""

import email.utils
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

HUBSPOT_TOKEN          = os.getenv("HUBSPOT_API_TOKEN", "")
MY_EMAIL               = os.getenv("MY_EMAIL", "").lower().strip()
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE       = os.getenv("GMAIL_TOKEN_FILE", "token.json")
STATE_FILE             = os.getenv("STATE_FILE", ".sync_state.json")
POLL_INTERVAL          = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
ENABLE_NOTES           = os.getenv("ENABLE_NOTES", "true").lower() != "false"
LEAD_SOURCE_PROPERTY   = os.getenv("LEAD_SOURCE_PROPERTY", "lead_source")

HUBSPOT_BASE = "https://api.hubapi.com"

# Free / personal domains — domain is not used as company name for these.
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "live.it",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "proton.me", "tutanota.com", "tutamail.com", "zoho.com", "yandex.com",
    "yandex.ru", "mail.com", "gmx.com", "gmx.it", "libero.it", "virgilio.it",
    "tiscali.it", "tin.it", "alice.it", "fastwebnet.it", "email.it",
    "msn.com", "inbox.com", "mail.ru",
}

# ── Gmail auth ────────────────────────────────────────────────────────────────


def gmail_service():
    creds: Optional[Credentials] = None
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


# ── State persistence ─────────────────────────────────────────────────────────


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"history_id": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ── Gmail helpers ─────────────────────────────────────────────────────────────


def fetch_new_inbox_messages(service, state: dict) -> tuple[list, str]:
    """
    Returns (messages, new_history_id).

    First run: sets history_id to current watermark and returns no messages
    so the operator's existing inbox is not bulk-imported into HubSpot.
    """
    profile = service.users().getProfile(userId="me").execute()
    current_hid = profile["historyId"]

    if state.get("history_id") is None:
        log.info(
            "Prima esecuzione — history_id inizializzato a %s. "
            "Solo i messaggi ricevuti d'ora in poi verranno processati.",
            current_hid,
        )
        return [], current_hid

    messages: list[dict] = []
    try:
        page_token: Optional[str] = None
        while True:
            kwargs = dict(
                userId="me",
                startHistoryId=state["history_id"],
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            if page_token:
                kwargs["pageToken"] = page_token

            history = service.users().history().list(**kwargs).execute()
            for record in history.get("history", []):
                for entry in record.get("messagesAdded", []):
                    msg = entry["message"]
                    labels = msg.get("labelIds", [])
                    # Keep only truly inbound messages
                    if "INBOX" in labels and "SENT" not in labels:
                        messages.append(msg)

            page_token = history.get("nextPageToken")
            if not page_token:
                break

    except HttpError as exc:
        if exc.resp.status == 404:
            # historyId expired (>30 days) — reset watermark
            log.warning("history_id scaduto, watermark resettato.")
            return [], current_hid
        raise

    return messages, current_hid


def get_from_header(service, message_id: str) -> Optional[str]:
    try:
        msg = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From"],
        ).execute()
        for hdr in msg.get("payload", {}).get("headers", []):
            if hdr["name"] == "From":
                return hdr["value"]
    except HttpError:
        pass
    return None


# ── Contact parsing ───────────────────────────────────────────────────────────


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Returns (email_address, first_name, last_name)."""
    display_name, addr = email.utils.parseaddr(from_header)
    addr = addr.lower().strip()
    first = last = ""
    if display_name:
        parts = display_name.strip().split(" ", 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""
    return addr, first, last


def domain_to_company(email_addr: str) -> tuple[str, str]:
    """Returns (domain, company_name). company_name is '' for personal domains."""
    try:
        domain = email_addr.split("@")[1].lower()
    except IndexError:
        return "", ""
    if domain in PERSONAL_DOMAINS:
        return domain, ""
    # Strip known TLD suffixes and title-case the result
    parts = domain.split(".")
    company = parts[0].replace("-", " ").title() if parts else ""
    return domain, company


# ── HubSpot helpers ───────────────────────────────────────────────────────────


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email_addr: str) -> Optional[dict]:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email_addr,
            }]
        }],
        "properties": ["email", "firstname", "lastname", "company", LEAD_SOURCE_PROPERTY],
        "limit": 1,
    }
    resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> str:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    resp = requests.post(url, headers=_hs_headers(), json={"properties": props}, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def hs_update_contact(contact_id: str, props: dict) -> str:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=_hs_headers(), json={"properties": props}, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def hs_create_note(contact_id: str, body: str) -> None:
    """Creates a note and associates it with the contact on the timeline."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    payload = {
        "properties": {
            "hs_timestamp": str(int(time.time() * 1000)),
            "hs_note_body": body,
        }
    }
    resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        log.debug("Note creation failed (%s): %s", resp.status_code, resp.text)
        return

    note_id = resp.json()["id"]
    assoc_url = (
        f"{HUBSPOT_BASE}/crm/v3/objects/notes/{note_id}"
        f"/associations/contacts/{contact_id}/note_to_contact"
    )
    requests.put(assoc_url, headers=_hs_headers(), timeout=30)


# ── Core sync logic ───────────────────────────────────────────────────────────


def sync_contact(
    email_addr: str,
    first_name: str,
    last_name: str,
    company: str,
    domain: str,
) -> tuple[str, str]:
    """
    Ensures the contact exists in HubSpot and is up to date.
    Returns (status, contact_id) where status ∈ {'created', 'updated', 'skipped'}.
    """
    existing = hs_find_contact(email_addr)

    new_props: dict = {"email": email_addr, LEAD_SOURCE_PROPERTY: "Gmail"}
    if first_name:
        new_props["firstname"] = first_name
    if last_name:
        new_props["lastname"] = last_name
    if company:
        new_props["company"] = company

    # ── New contact ────────────────────────────────────────────────────────
    if existing is None:
        contact_id = hs_create_contact(new_props)
        if ENABLE_NOTES:
            hs_create_note(
                contact_id,
                f"Email inbound ricevuta — mittente: {email_addr} "
                f"(dominio: {domain})\nFonte: Gmail | Tag: Inbound Gmail",
            )
        return "created", contact_id

    # ── Existing contact — patch only blank fields ─────────────────────────
    contact_id = existing["id"]
    ep = existing.get("properties", {})
    update_props: dict = {}

    if first_name and not ep.get("firstname"):
        update_props["firstname"] = first_name
    if last_name and not ep.get("lastname"):
        update_props["lastname"] = last_name
    if company and not ep.get("company"):
        update_props["company"] = company
    if not ep.get(LEAD_SOURCE_PROPERTY):
        update_props[LEAD_SOURCE_PROPERTY] = "Gmail"

    if update_props:
        hs_update_contact(contact_id, update_props)
        if ENABLE_NOTES:
            hs_create_note(
                contact_id,
                f"Nuova email inbound da {email_addr} — campi aggiornati: "
                f"{', '.join(update_props.keys())} | Tag: Inbound Gmail",
            )
        return "updated", contact_id

    return "skipped", contact_id


def process_message(service, message_id: str) -> Optional[dict]:
    from_header = get_from_header(service, message_id)
    if not from_header:
        return None

    email_addr, first_name, last_name = parse_sender(from_header)
    if not email_addr or "@" not in email_addr:
        return None
    if MY_EMAIL and email_addr == MY_EMAIL:
        return None  # skip self-sent messages

    domain, company = domain_to_company(email_addr)

    try:
        status, contact_id = sync_contact(email_addr, first_name, last_name, company, domain)
    except requests.HTTPError as exc:
        log.error("Errore HubSpot [%s]: %s", email_addr, exc)
        return {"status": "error", "email": email_addr, "contact_id": None, "error": str(exc)}

    return {"status": status, "email": email_addr, "contact_id": contact_id}


# ── Output formatting ─────────────────────────────────────────────────────────

_STATUS_LABELS = {
    "created": "CREATO    ",
    "updated": "AGGIORNATO",
    "skipped": "IGNORATO  ",
    "error":   "ERRORE    ",
}


def log_result(result: dict) -> None:
    label = _STATUS_LABELS.get(result["status"], result["status"].upper())
    extra = f" | Errore: {result['error']}" if result.get("error") else ""
    log.info(
        "Stato: %s | Email: %-40s | ID HubSpot: %s%s",
        label,
        result["email"],
        result.get("contact_id") or "N/A",
        extra,
    )


# ── Main loop ─────────────────────────────────────────────────────────────────


def main() -> None:
    if not HUBSPOT_TOKEN:
        log.error("HUBSPOT_API_TOKEN non impostato. Controlla le variabili d'ambiente.")
        raise SystemExit(1)

    if not Path(GMAIL_CREDENTIALS_FILE).exists():
        log.error(
            "File credenziali Gmail non trovato: %s\n"
            "Scaricalo da Google Cloud Console → API & Services → Credentials.",
            GMAIL_CREDENTIALS_FILE,
        )
        raise SystemExit(1)

    log.info(
        "Avvio Gmail → HubSpot sync | polling ogni %ds | note: %s",
        POLL_INTERVAL,
        "attivate" if ENABLE_NOTES else "disattivate",
    )

    service = gmail_service()
    state = load_state()

    while True:
        try:
            messages, new_hid = fetch_new_inbox_messages(service, state)

            if messages:
                # Deduplicate message IDs within the same poll window
                seen: set[str] = set()
                unique = [m for m in messages if not (m["id"] in seen or seen.add(m["id"]))]  # type: ignore[func-returns-value]
                log.info("Nuovi messaggi da processare: %d", len(unique))

                for msg in unique:
                    result = process_message(service, msg["id"])
                    if result:
                        log_result(result)

            state["history_id"] = new_hid
            save_state(state)

        except HttpError as exc:
            log.error("Errore Gmail API: %s", exc)
        except requests.RequestException as exc:
            log.error("Errore di rete: %s", exc)
        except KeyboardInterrupt:
            log.info("Sync interrotto dall'utente.")
            break
        except Exception as exc:  # noqa: BLE001
            log.exception("Errore inatteso: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
