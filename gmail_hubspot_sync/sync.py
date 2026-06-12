"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox, extracts senders, creates/updates HubSpot contacts.

Usage:
    python sync.py              # single run (last 24h)
    python sync.py --hours 48   # last 48 hours
    python sync.py --loop 300   # continuous loop every 5 minutes
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

# --- Optional deps (gracefully degrade if missing) ---
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False

try:
    import hubspot
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate
    from hubspot.crm.contacts.exceptions import ApiException
    HUBSPOT_AVAILABLE = True
except ImportError:
    HUBSPOT_AVAILABLE = False

# ─────────────────────────── Configuration ───────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(__file__).parent / ".sync_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hubspot_sync")

# ─────────────────────────── Helpers ─────────────────────────────────────────


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_message_ids": [], "last_run_ts": None}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _extract_name_parts(display_name: str) -> tuple[str, str]:
    """'Mario Rossi' → ('Mario', 'Rossi');  'mario' → ('mario', '')"""
    parts = display_name.strip().split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def _company_from_domain(domain: str) -> str:
    """'acme.com' → 'Acme';  'mail.google.com' → 'Google'"""
    # strip common prefixes and TLD
    parts = domain.lower().split(".")
    skip = {"mail", "m", "smtp", "info", "support", "hello", "no-reply", "noreply"}
    meaningful = [p for p in parts[:-1] if p not in skip]
    if meaningful:
        return meaningful[-1].capitalize()
    return domain.split(".")[0].capitalize()


def _is_blacklisted(email: str) -> bool:
    """Skip automated / no-reply senders."""
    patterns = [
        r"no.?reply",
        r"donotreply",
        r"noreply",
        r"mailer.daemon",
        r"postmaster",
        r"bounce",
        r"notification",
        r"automated",
    ]
    low = email.lower()
    return any(re.search(p, low) for p in patterns)


# ─────────────────────────── Gmail layer ─────────────────────────────────────


def get_gmail_service(token_file: str = "token.json", creds_file: str = "credentials.json"):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, hours_back: int = 24, max_results: int = 500) -> list[dict]:
    """Return list of {id, from_raw, subject, date} dicts."""
    after_ts = int(time.time()) - (hours_back * 3600)
    query = f"in:inbox after:{after_ts} -in:sent -in:drafts"
    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    messages = results.get("messages", [])
    parsed = []
    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        parsed.append({
            "id": msg["id"],
            "from_raw": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
        })
    return parsed


# ─────────────────────────── HubSpot layer ───────────────────────────────────


def get_hubspot_client(api_key: str | None = None) -> "hubspot.HubSpot":
    key = api_key or os.environ.get("HUBSPOT_API_KEY") or os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not key:
        raise EnvironmentError("Set HUBSPOT_API_KEY or HUBSPOT_ACCESS_TOKEN env variable.")
    return hubspot.HubSpot(access_token=key)


def find_contact_by_email(client, email: str) -> dict | None:
    """Return HubSpot contact dict or None."""
    from hubspot.crm.contacts import ApiException as ContactApiException
    try:
        resp = client.crm.contacts.basic_api.get_by_id(
            contact_id=email,
            id_property="email",
            properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        )
        return resp.to_dict()
    except ContactApiException as exc:
        if exc.status == 404:
            return None
        raise


def create_contact(client, props: dict) -> dict:
    payload = SimplePublicObjectInputForCreate(properties=props)
    return client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=payload
    ).to_dict()


def update_contact(client, contact_id: str, props: dict) -> dict:
    from hubspot.crm.contacts import SimplePublicObjectInput
    payload = SimplePublicObjectInput(properties=props)
    return client.crm.contacts.basic_api.update(
        contact_id=contact_id, simple_public_object_input=payload
    ).to_dict()


# ─────────────────────────── Core sync logic ─────────────────────────────────


def process_email(gmail_msg: dict, hubspot_client, state: dict) -> dict:
    """
    Returns a result dict:
        { status: 'created'|'updated'|'skipped', email, contact_id }
    """
    msg_id = gmail_msg["id"]
    from_raw = gmail_msg["from_raw"]

    display_name, email_addr = parseaddr(from_raw)
    email_addr = email_addr.lower().strip()

    if not email_addr or "@" not in email_addr:
        return {"status": "skipped", "reason": "no valid email", "email": from_raw, "contact_id": None}

    if _is_blacklisted(email_addr):
        return {"status": "skipped", "reason": "blacklisted sender", "email": email_addr, "contact_id": None}

    if msg_id in state["processed_message_ids"]:
        return {"status": "skipped", "reason": "already processed", "email": email_addr, "contact_id": None}

    domain = email_addr.split("@")[1]
    firstname, lastname = _extract_name_parts(display_name)
    company_name = _company_from_domain(domain)

    existing = find_contact_by_email(hubspot_client, email_addr)

    if existing:
        contact_id = existing["id"]
        # Only fill in missing fields
        updates = {}
        props = existing.get("properties", {})
        if not props.get("firstname") and firstname:
            updates["firstname"] = firstname
        if not props.get("lastname") and lastname:
            updates["lastname"] = lastname
        if not props.get("company") and company_name:
            updates["company"] = company_name
        if not props.get("hs_lead_source"):
            updates["hs_lead_source"] = "OTHER"  # Gmail

        if updates:
            update_contact(hubspot_client, contact_id, updates)
            status = "updated"
        else:
            status = "skipped"
            state["processed_message_ids"].append(msg_id)
            return {"status": status, "reason": "no new fields", "email": email_addr, "contact_id": contact_id}
    else:
        props = {
            "email": email_addr,
            "hs_lead_source": "OTHER",
        }
        if firstname:
            props["firstname"] = firstname
        if lastname:
            props["lastname"] = lastname
        if company_name:
            props["company"] = company_name

        new_contact = create_contact(hubspot_client, props)
        contact_id = new_contact["id"]
        status = "created"

    state["processed_message_ids"].append(msg_id)
    # Keep state list bounded
    if len(state["processed_message_ids"]) > 10_000:
        state["processed_message_ids"] = state["processed_message_ids"][-5_000:]

    return {"status": status, "email": email_addr, "contact_id": contact_id}


def run_sync(hours_back: int = 24) -> list[dict]:
    """One complete sync pass. Returns list of result dicts."""
    if not GMAIL_AVAILABLE:
        raise ImportError("Install google-api-python-client: pip install -r requirements.txt")
    if not HUBSPOT_AVAILABLE:
        raise ImportError("Install hubspot-api-client: pip install -r requirements.txt")

    state = _load_state()
    gmail_svc = get_gmail_service()
    hs_client = get_hubspot_client()

    messages = fetch_inbox_messages(gmail_svc, hours_back=hours_back)
    log.info("Found %d inbox messages to evaluate", len(messages))

    results = []
    for msg in messages:
        try:
            result = process_email(msg, hs_client, state)
            results.append(result)
            icon = {"created": "✅", "updated": "♻️", "skipped": "—"}.get(result["status"], "?")
            log.info("%s %-10s  %s  (id: %s)", icon, result["status"], result.get("email", ""), result.get("contact_id", ""))
        except Exception as exc:
            log.error("Error processing message %s: %s", msg.get("id"), exc)
            results.append({"status": "error", "email": msg.get("from_raw", ""), "contact_id": None, "error": str(exc)})

    state["last_run_ts"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    created = sum(1 for r in results if r["status"] == "created")
    updated = sum(1 for r in results if r["status"] == "updated")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    log.info("Done — created: %d  updated: %d  skipped: %d", created, updated, skipped)
    return results


# ─────────────────────────── CLI entry point ─────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--hours", type=int, default=24, help="Look back N hours (default: 24)")
    parser.add_argument("--loop", type=int, default=0,
                        help="Run continuously every N seconds (0 = single run)")
    args = parser.parse_args()

    if args.loop:
        log.info("Starting continuous sync loop every %ds", args.loop)
        while True:
            try:
                run_sync(hours_back=args.hours)
            except Exception as exc:
                log.error("Sync error: %s", exc)
            time.sleep(args.loop)
    else:
        run_sync(hours_back=args.hours)


if __name__ == "__main__":
    main()
