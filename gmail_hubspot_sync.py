#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors inbound Gmail messages and syncs sender contacts to HubSpot.
Avoids duplicates (email as unique key), fills missing fields, adds notes.

Usage:
    HUBSPOT_ACCESS_TOKEN=xxx python gmail_hubspot_sync.py

Required env vars:
    HUBSPOT_ACCESS_TOKEN    HubSpot Private App token
    GMAIL_CREDENTIALS_FILE  Path to Google OAuth2 credentials.json (default: credentials.json)
    GMAIL_TOKEN_FILE        Path to cached token (default: token.json)
    POLL_INTERVAL_SECONDS   Seconds between Gmail polls (default: 60)
    SYNC_STATE_FILE         Path to state JSON file (default: sync_state.json)

On first run, a browser window opens for Gmail OAuth2 authorization.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

# ── Third-party deps ──────────────────────────────────────────────────────────
# pip install google-auth google-auth-oauthlib google-api-python-client
#             hubspot-api-client
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
STATE_FILE = Path(os.getenv("SYNC_STATE_FILE", "sync_state.json"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Domains that belong to the user/organization — skip their outbound addresses
OWN_DOMAINS = {"latestata.it"}
OWN_ADDRESSES = {"pubblica.latestata@gmail.com", "cristian.mameli.editore@gmail.com"}

# Generic consumer email providers — no company can be derived from these
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "live.it",
    "libero.it", "alice.it", "tiscali.it", "virgilio.it", "tin.it",
    "icloud.com", "me.com", "protonmail.com", "pm.me",
    "fastwebnet.it", "inwind.it",
}

# Sender patterns that indicate automated / system mail — skip entirely
_SKIP_PATTERNS = (
    "mailer-daemon@", "noreply@", "no-reply@", "donotreply@",
    "notification@", "notifications@", "postmaster@", "bounce@",
    "facebookmail.com", "googlemail.com",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"history_id": None, "processed": []}


def save_state(state: dict) -> None:
    # Cap list to avoid unbounded growth
    state["processed"] = state["processed"][-20_000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def build_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _is_automated(email: str) -> bool:
    e = email.lower()
    return any(p in e for p in _SKIP_PATTERNS)


def _is_own(email: str) -> bool:
    domain = email.split("@")[-1].lower()
    return email.lower() in OWN_ADDRESSES or domain in OWN_DOMAINS


def _company_from_domain(email: str) -> str | None:
    domain = email.split("@")[-1].lower()
    if domain in GENERIC_DOMAINS:
        return None
    label = domain.split(".")[0]
    return label.capitalize() if label else None


def _parse_sender_name(display_name: str, email: str) -> tuple[str, str]:
    """Return (firstname, lastname) from display name or email local part."""
    name = display_name.strip()
    if not name:
        local = email.split("@")[0]
        name = local.replace(".", " ").replace("_", " ").replace("-", " ")
    parts = name.split(maxsplit=1)
    first = parts[0].capitalize()
    last = parts[1].capitalize() if len(parts) > 1 else ""
    return first, last


def fetch_new_messages(service, state: dict) -> list[str]:
    """Return a list of message IDs not yet processed."""
    message_ids: list[str] = []

    if state["history_id"]:
        try:
            page_token = None
            while True:
                kwargs = dict(
                    userId="me",
                    startHistoryId=state["history_id"],
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = service.users().history().list(**kwargs).execute()
                for record in resp.get("history", []):
                    for added in record.get("messagesAdded", []):
                        message_ids.append(added["message"]["id"])
                state["history_id"] = resp.get("historyId", state["history_id"])
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            return [mid for mid in message_ids if mid not in state["processed"]]
        except Exception as exc:
            log.warning("history.list failed (%s) — falling back to messages.list", exc)
            state["history_id"] = None

    # First run: list recent inbox messages
    resp = service.users().messages().list(
        userId="me", labelIds=["INBOX"], q="-from:me is:inbox", maxResults=50
    ).execute()
    all_ids = [m["id"] for m in resp.get("messages", [])]
    profile = service.users().getProfile(userId="me").execute()
    state["history_id"] = profile.get("historyId")
    return [mid for mid in all_ids if mid not in state["processed"]]


def get_sender_info(service, message_id: str) -> dict | None:
    """Fetch message metadata and return sender data dict, or None to skip."""
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
    except Exception as exc:
        log.error("messages.get(%s) failed: %s", message_id, exc)
        return None

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    raw_from = headers.get("From", "")
    display_name, email_address = parseaddr(raw_from)

    if not email_address or "@" not in email_address:
        return None
    email_address = email_address.lower().strip()

    if _is_automated(email_address) or _is_own(email_address):
        return None

    return {
        "message_id": message_id,
        "email": email_address,
        "display_name": display_name,
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
    }


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def build_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def hs_find_contact(client, email: str) -> dict | None:
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[
            Filter(property_name="email", operator="EQ", value=email)
        ])],
        properties=["email", "firstname", "lastname", "company"],
        limit=1,
    )
    try:
        results = client.crm.contacts.search_api.do_search(req).results
        return results[0].to_dict() if results else None
    except Exception as exc:
        log.error("HubSpot search error for %s: %s", email, exc)
        return None


def hs_create_contact(client, props: dict) -> str:
    obj = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=props, associations=[]
        )
    )
    return obj.id


def hs_update_contact(client, contact_id: str, props: dict) -> None:
    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=SimplePublicObjectInput(properties=props),
    )


def hs_add_gmail_note(client, contact_id: str, sender: dict) -> None:
    body = (
        f"📧 Email ricevuta via Gmail\n"
        f"Da: {sender['display_name']} <{sender['email']}>\n"
        f"Oggetto: {sender.get('subject', '–')}\n"
        f"Data: {sender.get('date', '–')}\n"
        f"Tag: Inbound Gmail"
    )
    ts = str(int(datetime.now(tz=timezone.utc).timestamp() * 1000))
    try:
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteInput(
                properties={"hs_note_body": body, "hs_timestamp": ts},
                associations=[{
                    "to": {"id": contact_id},
                    "types": [{
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # note → contact
                    }],
                }],
            )
        )
    except Exception as exc:
        log.warning("Could not add note to contact %s: %s", contact_id, exc)


# ── Core sync logic ───────────────────────────────────────────────────────────

def sync_sender(client, sender: dict) -> dict:
    """Upsert a contact in HubSpot. Returns a result dict."""
    email = sender["email"]
    first, last = _parse_sender_name(sender["display_name"], email)
    company = _company_from_domain(email)

    existing = hs_find_contact(client, email)

    if existing:
        contact_id = existing["id"]
        ep = existing.get("properties", {})
        updates: dict[str, str] = {}
        if not ep.get("firstname") and first:
            updates["firstname"] = first
        if not ep.get("lastname") and last:
            updates["lastname"] = last
        if not ep.get("company") and company:
            updates["company"] = company

        if updates:
            hs_update_contact(client, contact_id, updates)
            action = "Aggiornato"
        else:
            action = "Ignorato"

        hs_add_gmail_note(client, contact_id, sender)
        return {"status": action, "email": email, "hubspot_id": contact_id}

    # New contact
    props: dict[str, str] = {
        "email": email,
        "firstname": first,
        "lifecyclestage": "lead",
        "hs_analytics_source": "OTHER_CAMPAIGNS",
    }
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company

    contact_id = hs_create_contact(client, props)
    hs_add_gmail_note(client, contact_id, sender)
    return {"status": "Creato", "email": email, "hubspot_id": contact_id}


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once(gmail, hs_client, state: dict) -> list[dict]:
    message_ids = fetch_new_messages(gmail, state)
    log.info("Nuovi messaggi da elaborare: %d", len(message_ids))

    results: list[dict] = []
    seen: set[str] = set()

    for msg_id in message_ids:
        sender = get_sender_info(gmail, msg_id)
        state["processed"].append(msg_id)

        if not sender or sender["email"] in seen:
            continue
        seen.add(sender["email"])

        result = sync_sender(hs_client, sender)
        results.append(result)
        log.info("[%s] %s → ID %s", result["status"], result["email"], result["hubspot_id"])

    save_state(state)
    return results


def print_report(results: list[dict]) -> None:
    if not results:
        print("Nessun nuovo contatto da sincronizzare.")
        return
    w = max(len(r["email"]) for r in results)
    print("\n── Risultati sincronizzazione ─────────────────────────────────────────")
    print(f"  {'Stato':<12}  {'Email':<{w}}  ID HubSpot")
    print(f"  {'─'*12}  {'─'*w}  {'─'*15}")
    for r in results:
        print(f"  {r['status']:<12}  {r['email']:<{w}}  {r['hubspot_id']}")
    print()


def main() -> None:
    if not HUBSPOT_TOKEN:
        raise SystemExit(
            "Imposta la variabile d'ambiente HUBSPOT_ACCESS_TOKEN prima di avviare."
        )

    gmail = build_gmail_service()
    hs_client = build_hubspot_client()
    state = load_state()

    log.info("Avvio loop Gmail→HubSpot (intervallo: %ds)", POLL_INTERVAL)
    try:
        while True:
            try:
                results = run_once(gmail, hs_client, state)
                print_report(results)
            except Exception as exc:
                log.error("Errore durante la sincronizzazione: %s", exc, exc_info=True)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Interrotto dall'utente.")


if __name__ == "__main__":
    main()
