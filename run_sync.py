"""
run_sync.py – esegue la sincronizzazione Gmail → HubSpot usando i tool MCP
disponibili nell'ambiente Claude Code.

Avvia con:
    python run_sync.py            # scansione singola
    python run_sync.py --watch 60 # polling ogni 60 secondi
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP tool wrappers
# These functions call the real MCP endpoints provided by the Claude Code
# environment.  If a tool is unavailable the wrapper raises RuntimeError so
# the caller can surface a clear error message.
# ---------------------------------------------------------------------------

def _gmail_search_threads(query: str = "in:inbox", maxResults: int = 50) -> dict:
    """Call mcp__Gmail__search_threads."""
    from mcp__Gmail__search_threads import search_threads  # type: ignore
    return search_threads(query=query, maxResults=maxResults)


def _gmail_get_thread(thread_id: str) -> dict:
    """Call mcp__Gmail__get_thread."""
    from mcp__Gmail__get_thread import get_thread  # type: ignore
    return get_thread(thread_id=thread_id)


def _hubspot_search_crm_objects(**kwargs) -> dict:
    """Call mcp__HubSpot__search_crm_objects."""
    from mcp__HubSpot__search_crm_objects import search_crm_objects  # type: ignore
    return search_crm_objects(**kwargs)


def _hubspot_manage_crm_objects(**kwargs) -> dict:
    """Call mcp__HubSpot__manage_crm_objects."""
    from mcp__HubSpot__manage_crm_objects import manage_crm_objects  # type: ignore
    return manage_crm_objects(**kwargs)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync (MCP)")
    parser.add_argument("--watch", metavar="SECONDS", type=int, default=0,
                        help="Polling interval in seconds (0 = one-shot)")
    parser.add_argument("--max-results", metavar="N", type=int, default=50,
                        help="Max Gmail threads per scan (default: 50)")
    args = parser.parse_args(argv)

    try:
        import gmail_hubspot_mcp as mcp_runner
        mcp_runner.run(
            gmail_search_fn=_gmail_search_threads,
            gmail_get_thread_fn=_gmail_get_thread,
            hubspot_search_fn=_hubspot_search_crm_objects,
            hubspot_manage_fn=_hubspot_manage_crm_objects,
            watch=args.watch,
            max_results=args.max_results,
        )
    except ImportError as exc:
        log.error("MCP tool not available: %s", exc)
        log.error("Assicurati di eseguire questo script nell'ambiente Claude Code "
                  "con i server MCP Gmail e HubSpot configurati.")
        sys.exit(1)


if __name__ == "__main__":
    main()
