"""
Agent entrypoint — called by the Claude Code agent to start the Gmail→HubSpot sync.

In a real Claude Code session the MCP tools (mcp__Gmail__, mcp__HubSpot__) are
injected automatically.  This file wires them up and starts the main loop.

For local testing without live MCP credentials, set MOCK_MCP=1:
    MOCK_MCP=1 python agent_entrypoint.py
"""

import os
import logging
from gmail_hubspot_sync import (
    GmailBridge,
    HubSpotBridge,
    main_loop,
    run_once,
    SyncResult,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mock implementations for local / offline testing
# ---------------------------------------------------------------------------

class MockGmailMCP:
    """Returns a fake inbox with two sample senders."""

    def search_threads(self, query: str, maxResults: int = 50) -> dict:
        return {
            "threads": [
                {"id": "thread_001"},
                {"id": "thread_002"},
                {"id": "thread_003"},
            ]
        }

    def get_thread(self, threadId: str) -> dict:
        samples = {
            "thread_001": {
                "messages": [
                    {
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "Mario Rossi <mario.rossi@acmecorp.it>"}
                            ]
                        }
                    }
                ]
            },
            "thread_002": {
                "messages": [
                    {
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "newsletter@gmail.com"}
                            ]
                        }
                    }
                ]
            },
            "thread_003": {
                "messages": [
                    {
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "Lucia Bianchi <l.bianchi@startup.io>"}
                            ]
                        }
                    }
                ]
            },
        }
        return samples.get(threadId, {"messages": []})


class MockHubSpotMCP:
    """Simulates a HubSpot CRM with one pre-existing contact."""

    def __init__(self):
        self._contacts = {
            "mario.rossi@acmecorp.it": {
                "id": "hs_001",
                "properties": {
                    "email": "mario.rossi@acmecorp.it",
                    "firstname": "Mario",
                    "lastname": "",
                    "company": "",
                    "hs_lead_source": "",
                },
            }
        }
        self._next_id = 100

    def search_crm_objects(self, object_type: str, filter_groups: list, **kwargs) -> dict:
        if object_type != "contacts":
            return {"results": []}
        email = filter_groups[0]["filters"][0]["value"]
        contact = self._contacts.get(email)
        return {"results": [contact] if contact else []}

    def manage_crm_objects(self, object_type: str, action: str, **kwargs) -> dict:
        if object_type == "notes":
            log.info("[MOCK] HubSpot note created for association.")
            return {"id": "note_mock"}

        if object_type != "contacts":
            return {"id": "mock"}

        if action == "create":
            props = kwargs.get("properties", {})
            email = props.get("email", "")
            new_id = str(self._next_id)
            self._next_id += 1
            self._contacts[email] = {"id": new_id, "properties": props}
            log.info("[MOCK] HubSpot contact CREATED id=%s email=%s", new_id, email)
            return {"id": new_id}

        if action == "update":
            obj_id = kwargs.get("object_id", "")
            props = kwargs.get("properties", {})
            for contact in self._contacts.values():
                if contact["id"] == obj_id:
                    contact["properties"].update(props)
                    log.info("[MOCK] HubSpot contact UPDATED id=%s props=%s", obj_id, list(props))
                    return {"id": obj_id}
        return {}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def start(once: bool = False, add_activity: bool = True) -> list[SyncResult] | None:
    use_mock = os.environ.get("MOCK_MCP", "0") == "1"

    if use_mock:
        log.warning("Running with MOCK MCP tools — no real Gmail/HubSpot calls made.")
        gmail_mcp = MockGmailMCP()
        hubspot_mcp = MockHubSpotMCP()
    else:
        # In a live Claude Code session, the MCP tools are available as module-level
        # objects injected by the framework.  Import them here at runtime.
        try:
            import mcp__Gmail__ as gmail_mcp          # noqa: F401
            import mcp__HubSpot__ as hubspot_mcp      # noqa: F401
        except ImportError:
            raise RuntimeError(
                "MCP tools not found. Run with MOCK_MCP=1 for local testing, "
                "or launch this script from within a Claude Code agent session."
            )

    gmail = GmailBridge(gmail_mcp)
    hubspot = HubSpotBridge(hubspot_mcp)

    if once:
        return run_once(gmail, hubspot, add_activity=add_activity)
    else:
        main_loop(gmail, hubspot, add_activity=add_activity)
        return None


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Gmail → HubSpot agent entrypoint")
    parser.add_argument("--once", action="store_true", help="Single cycle then exit")
    parser.add_argument("--no-activity", action="store_true", help="Skip HubSpot notes")
    args = parser.parse_args()

    results = start(once=True, add_activity=not args.no_activity)
    if results:
        print("\n=== SYNC RESULTS ===")
        for r in results:
            print(f"  Status: {r.status.upper():<8}  Email: {r.email:<35}  HubSpot ID: {r.contact_id or 'N/A'}")
