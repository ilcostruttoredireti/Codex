"""
Gmail → HubSpot Agent Runner
Wires the MCP tool calls to gmail_hubspot_sync and starts the loop.
This file is the entry-point when running inside Claude Code / MCP context.
"""

from __future__ import annotations
import logging
from gmail_hubspot_sync import main, run_sync_cycle, print_cycle_report

log = logging.getLogger(__name__)


class MCPClient:
    """
    Thin shim that routes mcp.call(tool_name, **kwargs) to the real MCP
    tool functions available in this session.

    The actual MCP tools (mcp__Gmail__*, mcp__HubSpot__*) are bound at
    import time from the host environment.  This class makes them callable
    with a uniform interface so gmail_hubspot_sync.py stays testable.
    """

    def __init__(self, tool_map: dict):
        self._tools = tool_map

    def call(self, tool_name: str, **kwargs):
        fn = self._tools.get(tool_name)
        if fn is None:
            raise RuntimeError(f"MCP tool not available: {tool_name}")
        return fn(**kwargs)


def build_tool_map() -> dict:
    """
    Import MCP tools from the host environment and return them as a dict.
    The keys match the strings used in gmail_hubspot_sync.py.
    """
    try:
        from mcp__Gmail__search_threads import search_threads          # noqa: F401
        from mcp__Gmail__get_thread import get_thread                  # noqa: F401
        from mcp__HubSpot__search_crm_objects import search_crm_objects  # noqa: F401
        from mcp__HubSpot__manage_crm_objects import manage_crm_objects  # noqa: F401
    except ImportError:
        # Running outside the MCP host — use the stub below for development
        log.warning("MCP tools not found in environment. Using stubs for local dev.")
        return _stub_tool_map()

    return {
        "mcp__Gmail__search_threads": search_threads,
        "mcp__Gmail__get_thread": get_thread,
        "mcp__HubSpot__search_crm_objects": search_crm_objects,
        "mcp__HubSpot__manage_crm_objects": manage_crm_objects,
    }


def _stub_tool_map() -> dict:
    """Minimal stubs for offline development/testing."""

    def search_threads(**kw):
        return {"threads": []}

    def get_thread(**kw):
        return {"id": kw.get("threadId"), "messages": []}

    def search_crm_objects(**kw):
        return {"results": []}

    def manage_crm_objects(**kw):
        return {"results": [{"id": "STUB-ID"}]}

    return {
        "mcp__Gmail__search_threads": search_threads,
        "mcp__Gmail__get_thread": get_thread,
        "mcp__HubSpot__search_crm_objects": search_crm_objects,
        "mcp__HubSpot__manage_crm_objects": manage_crm_objects,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    args = parser.parse_args()

    mcp = MCPClient(build_tool_map())
    main(mcp, once=args.once)
