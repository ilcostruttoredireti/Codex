#!/usr/bin/env python3
"""Gmail → HubSpot contact sync agent using Anthropic MCP client beta."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

STATE_FILE_DEFAULT = Path(".gmail_hubspot_state.json")
MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are a Gmail-to-HubSpot contact sync agent. Your job is to:
1. Search Gmail for recent inbox emails (use query: in:inbox newer_than:14d).
2. For each email, extract sender data: email address, first name, last name (if available), company domain (from email domain, skip generic providers like gmail.com, yahoo.com, hotmail.com, outlook.com, icloud.com, live.com, me.com, msn.com).
3. Check HubSpot for an existing contact by email (use search_crm_objects with objectType=contacts, filterGroups on email property).
4. If contact EXISTS: update only fields that are currently empty/missing (do not overwrite existing values). Set stato=Aggiornato. If nothing needed updating, set stato=Ignorato.
5. If contact DOES NOT EXIST: create a new contact with: email, firstname, lastname, company (derived from domain if not generic), hs_lead_status=NEW, lead_source=GMAIL. Set stato=Creato.
6. Optionally add a HubSpot timeline note: "Inbound email from Gmail".

Rules:
- Use the sender's email as the unique key — never create duplicates.
- Skip emails where the sender is yourself (noreply@, no-reply@, donotreply@, mailer-daemon@, bounce@, postmaster@).
- Process at most MAX_EMAILS emails per run (respect the limit given in the task).
- Already-processed thread IDs must be skipped entirely.

Return ONLY a valid JSON array (no markdown, no extra text) in this exact format:
[
  {
    "thread_id": "<gmail thread id>",
    "stato": "Creato|Aggiornato|Ignorato",
    "email_contatto": "<sender email>",
    "hubspot_id": "<hubspot contact id or null>",
    "note": "<brief reason, e.g. 'new contact created' or 'email already known, name updated'>"
  }
]
If there are no emails to process, return an empty array: []
"""

TASK_TEMPLATE = """\
Sync Gmail contacts to HubSpot. Process at most {max_emails} new emails.

Already processed thread IDs — SKIP THESE ENTIRELY (do not call get_thread for them):
{processed_ids}

Steps:
1. Call Gmail search_threads with query "in:inbox newer_than:14d" to get recent threads.
2. Filter out any thread IDs listed above.
3. For each remaining thread (up to {max_emails}), call get_thread to get full details.
4. Extract sender from the From: header of the first message.
5. Process each sender against HubSpot as described in your instructions.
6. Return the JSON result array.
"""


@dataclass
class MCPServer:
    name: str
    url: str
    server_id: str
    session_uuid: str

    def to_dict(self) -> dict:
        return {
            "type": "url",
            "url": self.url,
            "name": self.name,
            "headers": {
                "X-MCP-Server-ID": self.server_id,
                "X-Session-UUID": self.session_uuid,
            },
        }


def _load_mcp_config_file(path: Path) -> tuple[MCPServer, MCPServer]:
    with path.open() as f:
        cfg = json.load(f)
    servers = cfg.get("mcpServers", {})

    def _build(key: str) -> MCPServer:
        s = servers[key]
        return MCPServer(
            name=key,
            url=s["url"],
            server_id=s["headers"]["X-MCP-Server-ID"],
            session_uuid=s["headers"]["X-Session-UUID"],
        )

    return _build("Gmail"), _build("HubSpot")


def load_mcp_servers() -> tuple[MCPServer, MCPServer]:
    """Return (gmail, hubspot) MCP server configs.

    Resolution order:
    1. MCP_CONFIG_PATH env var
    2. Auto-detect /tmp/mcp-config-cse_*.json
    3. Individual env vars (GMAIL_MCP_URL, etc.)
    """
    # Option 1: explicit config path
    config_path = os.getenv("MCP_CONFIG_PATH")
    if config_path:
        return _load_mcp_config_file(Path(config_path))

    # Option 2: auto-detect session config
    candidates = sorted(Path("/tmp").glob("mcp-config-cse_*.json"))
    if candidates:
        return _load_mcp_config_file(candidates[-1])

    # Option 3: explicit env vars
    session_uuid = os.getenv("MCP_SESSION_UUID", "")
    gmail = MCPServer(
        name="Gmail",
        url=os.environ["GMAIL_MCP_URL"],
        server_id=os.environ["GMAIL_MCP_SERVER_ID"],
        session_uuid=session_uuid,
    )
    hubspot = MCPServer(
        name="HubSpot",
        url=os.environ["HUBSPOT_MCP_URL"],
        server_id=os.environ["HUBSPOT_MCP_SERVER_ID"],
        session_uuid=session_uuid,
    )
    return gmail, hubspot


def load_state(state_file: Path) -> dict:
    if state_file.exists():
        with state_file.open() as f:
            return json.load(f)
    return {"processed_thread_ids": []}


def save_state(state: dict, state_file: Path) -> None:
    with state_file.open("w") as f:
        json.dump(state, f, indent=2)


def _extract_json(text: str) -> list[dict]:
    """Extract JSON array from model response text."""
    text = text.strip()
    # find first '[' and last ']'
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []
    return json.loads(text[start : end + 1])


def run_sync(
    client: anthropic.Anthropic,
    gmail: MCPServer,
    hubspot: MCPServer,
    processed_ids: list[str],
    max_emails: int = 20,
) -> list[dict]:
    task = TASK_TEMPLATE.format(
        max_emails=max_emails,
        processed_ids=json.dumps(processed_ids) if processed_ids else "[]",
    )

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=8096,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": task}],
        betas=["mcp-client-2025-04-04"],
        mcp_servers=[gmail.to_dict(), hubspot.to_dict()],  # type: ignore[arg-type]
    )

    # Extract text from final response
    result_text = ""
    for block in response.content:
        if hasattr(block, "type") and block.type == "text":
            result_text = block.text
            break

    return _extract_json(result_text)


def print_results(results: list[dict]) -> None:
    if not results:
        print("  (nessuna email da processare)")
        return

    stato_icons = {
        "Creato": "✅",
        "Aggiornato": "🔄",
        "Ignorato": "⏭️",
    }

    print(f"\n  {'STATO':<12} {'EMAIL CONTATTO':<35} {'HUBSPOT ID':<20} NOTE")
    print(f"  {'-'*12} {'-'*35} {'-'*20} {'-'*30}")
    for r in results:
        icon = stato_icons.get(r.get("stato", ""), "❓")
        stato = f"{icon} {r.get('stato', '?')}"
        email = r.get("email_contatto", "-")
        hid = r.get("hubspot_id") or "-"
        note = r.get("note", "")
        print(f"  {stato:<14} {email:<35} {hid:<20} {note}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gmail → HubSpot contact sync agent"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Run continuously, polling at --interval seconds",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        help="Poll interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--max-emails",
        type=int,
        default=int(os.getenv("MAX_EMAILS_PER_RUN", "20")),
        help="Max emails to process per run (default: 20)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=STATE_FILE_DEFAULT,
        help=f"State file path (default: {STATE_FILE_DEFAULT})",
    )
    parser.add_argument(
        "--clear-state",
        action="store_true",
        help="Clear processed thread IDs state before running",
    )
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    try:
        gmail, hubspot = load_mcp_servers()
    except (KeyError, FileNotFoundError) as e:
        print(f"Error loading MCP server config: {e}", file=sys.stderr)
        print(
            "Set MCP_CONFIG_PATH or ensure /tmp/mcp-config-cse_*.json exists",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.clear_state and args.state_file.exists():
        args.state_file.unlink()
        print(f"Cleared state file: {args.state_file}")

    run_count = 0
    while True:
        run_count += 1
        state = load_state(args.state_file)
        processed_ids: list[str] = state.get("processed_thread_ids", [])

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{ts}] Run #{run_count} — {len(processed_ids)} thread(s) already processed")

        try:
            results = run_sync(
                client=client,
                gmail=gmail,
                hubspot=hubspot,
                processed_ids=processed_ids,
                max_emails=args.max_emails,
            )
        except anthropic.APIError as e:
            print(f"  API error: {e}", file=sys.stderr)
            if not args.watch:
                sys.exit(1)
        else:
            print_results(results)

            # Update state with newly processed thread IDs
            new_ids = [r["thread_id"] for r in results if r.get("thread_id")]
            if new_ids:
                state["processed_thread_ids"] = list(
                    dict.fromkeys(processed_ids + new_ids)  # deduplicate, preserve order
                )
                save_state(state, args.state_file)

            creati = sum(1 for r in results if r.get("stato") == "Creato")
            aggiornati = sum(1 for r in results if r.get("stato") == "Aggiornato")
            ignorati = sum(1 for r in results if r.get("stato") == "Ignorato")
            print(
                f"  Sommario: {creati} creati, {aggiornati} aggiornati, {ignorati} ignorati"
            )

        if not args.watch:
            break

        print(f"  Prossimo run tra {args.interval}s (Ctrl+C per uscire)")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nInterrotto.")
            break


if __name__ == "__main__":
    main()
