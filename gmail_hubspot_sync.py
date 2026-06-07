"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot.
Uses email address as the unique deduplication key.
"""

import re
import time
import json
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / ".sync_state.json"
POLL_INTERVAL_SECONDS = 60
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Domains that belong to personal / generic providers → no company inferred
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "live.com", "msn.com", "me.com", "aol.com", "protonmail.com",
    "libero.it", "virgilio.it", "alice.it", "tiscali.it",
}

# Internal senders that forward external press releases — we parse the body
FORWARDING_ADDRESSES = {
    "redazione@latestata.it",
    "pubblica.latestata@gmail.com",
}

# Senders to skip entirely (system, daemons, bounce handlers)
SKIP_SENDER_PATTERNS = re.compile(
    r"(mailer-daemon|postmaster|noreply|no-reply|posta-certificata|legalmail|"
    r"bounces\+|bounce\+|auto-reply|autoresponder)",
    re.IGNORECASE,
)


# ─────────────────────────── State helpers ────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_thread_ids": [], "last_run_ts": 0}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─────────────────────────── Parsing helpers ──────────────────────────────────

def _parse_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (firstname, lastname). Best-effort."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    """Derive a company name from an email domain, skipping personal providers."""
    if not domain or domain.lower() in PERSONAL_DOMAINS:
        return ""
    name = domain.split(".")[0]
    return name.capitalize()


def _extract_sender(from_header: str) -> Optional[dict]:
    """
    Parse a From: header and return a dict with:
        email, display_name, firstname, lastname, domain, company
    Returns None if the header is empty or unparseable.
    """
    display_name, email = parseaddr(from_header)
    email = email.strip().lower()
    if not email or "@" not in email:
        return None

    domain = email.split("@")[1]
    firstname, lastname = _parse_name(display_name)
    company = _company_from_domain(domain)

    return {
        "email": email,
        "display_name": display_name,
        "firstname": firstname,
        "lastname": lastname,
        "domain": domain,
        "company": company,
    }


# ─────────────────────────── Gmail helpers ────────────────────────────────────

def fetch_new_inbox_threads(mcp, already_seen: set[str]) -> list[dict]:
    """
    Return threads from INBOX that haven't been processed yet.
    Each item is the raw thread object from the MCP response.
    """
    result = mcp.call(
        "mcp__Gmail__search_threads",
        query="in:inbox",
        pageSize=50,
    )
    threads = result.get("threads", [])
    new_threads = [t for t in threads if t.get("id") not in already_seen]
    log.info("Gmail: %d total threads, %d new to process", len(threads), len(new_threads))
    return new_threads


def _parse_forwarded_sender(snippet: str) -> Optional[str]:
    """
    Extract a 'Da "Name" email' or 'From: Name <email>' pattern from a
    forwarded-message snippet so we capture the original external sender.
    Returns a From:-style string like '"Name" <email>' or None.
    """
    # Italian: Da "Name" email@domain or Da: Name <email>
    m = re.search(r'Da\s+"([^"]+)"\s+([\w.+\-]+@[\w.\-]+)', snippet)
    if m:
        return f'"{m.group(1)}" <{m.group(2)}>'
    m = re.search(r'Da:\s+([^<\n]+?)\s+<([\w.+\-]+@[\w.\-]+)>', snippet)
    if m:
        return f'"{m.group(1).strip()}" <{m.group(2)}>'
    # English: From: Name <email>
    m = re.search(r'From:\s+([^<\n]+?)\s+<([\w.+\-]+@[\w.\-]+)>', snippet)
    if m:
        return f'"{m.group(1).strip()}" <{m.group(2)}>'
    return None


def extract_senders_from_thread(thread: dict) -> list[str]:
    """
    Return a list of From:-style strings for every *real external* sender
    found in the thread (direct sender + embedded forwarded senders).
    Skips system/daemon addresses and internal forwarding addresses.
    """
    results: list[str] = []
    messages = thread.get("messages", [])

    for msg in messages:
        sender_email = msg.get("sender", "")
        snippet = msg.get("snippet", "")

        # Skip system / daemon senders
        if SKIP_SENDER_PATTERNS.search(sender_email):
            continue

        if sender_email.lower() in FORWARDING_ADDRESSES:
            # Mine the snippet for the embedded original sender
            fwd = _parse_forwarded_sender(snippet)
            if fwd:
                results.append(fwd)
        else:
            # Direct external sender — rebuild a From: string
            results.append(sender_email)

    return results


# ─────────────────────────── HubSpot helpers ──────────────────────────────────

def find_contact_by_email(mcp, email: str) -> Optional[dict]:
    """Return the existing HubSpot contact dict, or None."""
    result = mcp.call(
        "mcp__HubSpot__search_crm_objects",
        objectType="contacts",
        filterGroups=[{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        chatInsights={"userIntent": "find contact by email", "satisfaction": "NEUTRAL"},
    )
    results = result.get("results", [])
    return results[0] if results else None


def build_contact_properties(sender: dict, existing: Optional[dict]) -> dict:
    """
    Build the HubSpot properties dict for a create or update operation.
    For updates, only set fields that are currently blank in HubSpot.
    """
    props: dict[str, str] = {}

    existing_props = (existing or {}).get("properties", {})

    def _needs(key: str) -> bool:
        return not existing_props.get(key)

    if _needs("email"):
        props["email"] = sender["email"]
    if _needs("firstname") and sender["firstname"]:
        props["firstname"] = sender["firstname"]
    if _needs("lastname") and sender["lastname"]:
        props["lastname"] = sender["lastname"]
    if _needs("company") and sender["company"]:
        props["company"] = sender["company"]

    # Always set / refresh source and lead status
    props["hs_lead_status"] = "NEW"
    # hs_analytics_source_data_1 = drill-down label visible in HubSpot as "Original Source Data 1"
    if _needs("hs_analytics_source"):
        props["hs_analytics_source"] = "OFFLINE"
    if _needs("hs_analytics_source_data_1"):
        props["hs_analytics_source_data_1"] = CONTACT_SOURCE

    return props


def create_contact(mcp, sender: dict) -> dict:
    """Create a new HubSpot contact and return the created object."""
    props = build_contact_properties(sender, existing=None)
    result = mcp.call(
        "mcp__HubSpot__manage_crm_objects",
        confirmationStatus="CONFIRMATION_WAIVED_FOR_SESSION",
        createRequest={
            "objects": [{
                "objectType": "contacts",
                "properties": props,
            }]
        },
    )
    return result.get("results", [{}])[0]


def update_contact(mcp, contact_id: int, sender: dict, existing: dict) -> dict:
    """Fill in any blank fields on an existing HubSpot contact."""
    props = build_contact_properties(sender, existing=existing)
    # Remove 'email' from updates — it's the key, not something we patch
    props.pop("email", None)

    if not props:
        return existing  # nothing to update

    result = mcp.call(
        "mcp__HubSpot__manage_crm_objects",
        confirmationStatus="CONFIRMATION_WAIVED_FOR_SESSION",
        updateRequest={
            "objects": [{
                "objectType": "contacts",
                "objectId": contact_id,
                "properties": props,
            }]
        },
    )
    return result.get("results", [{}])[0]


# ─────────────────────────── Core sync logic ──────────────────────────────────

def process_thread(mcp, thread: dict) -> list[dict]:
    """
    Process a single Gmail thread.
    A thread may contain multiple unique senders (e.g. direct reply + forwarded).
    Returns a list of result dicts: [{status, email, hubspot_id}, ...]
    """
    thread_id = thread.get("id", "?")

    from_strings = extract_senders_from_thread(thread)
    if not from_strings:
        log.debug("Thread %s: no external senders found, skipping", thread_id)
        return [{"status": "Ignorato", "email": None, "hubspot_id": None}]

    results = []
    seen_in_thread: set[str] = set()

    for from_str in from_strings:
        sender = _extract_sender(from_str)
        if not sender:
            continue
        email = sender["email"]
        if email in seen_in_thread:
            continue
        seen_in_thread.add(email)

        log.info("Thread %s: processing <%s>", thread_id, email)
        existing = find_contact_by_email(mcp, email)

        if existing:
            contact_id = int(existing["id"])
            update_contact(mcp, contact_id, sender, existing)
            log.info("  → Aggiornato  | ID %s | %s", contact_id, email)
            results.append({"status": "Aggiornato", "email": email, "hubspot_id": contact_id})
        else:
            created = create_contact(mcp, sender)
            contact_id = created.get("id") or created.get("objectId")
            log.info("  → Creato      | ID %s | %s", contact_id, email)
            results.append({"status": "Creato", "email": email, "hubspot_id": contact_id})

    return results or [{"status": "Ignorato", "email": None, "hubspot_id": None}]


def run_sync_cycle(mcp) -> list[dict]:
    """
    One full sync cycle: fetch new threads → process each → persist state.
    Returns the list of result dicts for this cycle.
    """
    state = _load_state()
    already_seen: set[str] = set(state["processed_thread_ids"])

    new_threads = fetch_new_inbox_threads(mcp, already_seen)

    results = []
    for thread in new_threads:
        thread_id = thread.get("id")
        try:
            thread_results = process_thread(mcp, thread)
        except Exception as exc:
            log.error("Thread %s: unexpected error — %s", thread_id, exc)
            thread_results = [{"status": "Errore", "email": None, "hubspot_id": None, "error": str(exc)}]

        results.extend(thread_results)

        # Mark as seen regardless of outcome to avoid reprocessing on errors
        already_seen.add(thread_id)

    state["processed_thread_ids"] = list(already_seen)
    state["last_run_ts"] = int(datetime.now(timezone.utc).timestamp())
    _save_state(state)

    return results


def print_cycle_report(results: list[dict]) -> None:
    if not results:
        log.info("Nessuna nuova email da elaborare.")
        return
    print("\n" + "─" * 55)
    print(f"{'STATO':<12}  {'EMAIL CONTATTO':<32}  {'ID HUBSPOT'}")
    print("─" * 55)
    for r in results:
        print(f"{r['status']:<12}  {str(r.get('email') or '-'):<32}  {r.get('hubspot_id') or '-'}")
    print("─" * 55 + "\n")


# ─────────────────────────── Entry point ──────────────────────────────────────

def main(mcp, *, once: bool = False) -> None:
    """
    Main loop.

    Args:
        mcp:  The MCP client object injected by the calling environment.
        once: If True, run a single cycle then exit (useful for testing / CI).
    """
    log.info("Gmail → HubSpot sync avviato. Poll ogni %ds.", POLL_INTERVAL_SECONDS)
    while True:
        log.info("═" * 40)
        log.info("Avvio ciclo di sincronizzazione — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        try:
            results = run_sync_cycle(mcp)
            print_cycle_report(results)
        except Exception as exc:
            log.error("Errore nel ciclo di sync: %s", exc)

        if once:
            break
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(
        "Eseguire tramite l'agente MCP (vedi README). "
        "Non eseguire direttamente con Python."
    )
