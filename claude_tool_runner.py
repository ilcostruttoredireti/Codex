"""
Claude MCP Tool Runner
Bridges the gmail_hubspot_sync module to real MCP tool calls
when executed inside the Claude Code / Agent runtime.

Usage (from CLAUDE.md task or agent invocation):
    from claude_tool_runner import MCPClient, run_once, run_loop
"""

import logging
import time
from typing import Optional

from gmail_hubspot_sync import (
    SyncResult,
    fetch_recent_threads,
    process_thread,
)

logger = logging.getLogger(__name__)


class _GmailMCP:
    """Thin adapter that maps method calls to real MCP Gmail tools."""

    def __init__(self, call_tool):
        self._call = call_tool

    def search_threads(self, query: str, max_results: int = 50) -> dict:
        return self._call(
            "mcp__Gmail__search_threads",
            query=query,
            maxResults=max_results,
        )

    def get_thread(self, thread_id: str) -> dict:
        return self._call("mcp__Gmail__get_thread", threadId=thread_id)


class _HubSpotMCP:
    """Thin adapter that maps method calls to real MCP HubSpot tools."""

    def __init__(self, call_tool):
        self._call = call_tool

    def search_crm_objects(
        self,
        object_type: str,
        filter_groups: list,
        properties: Optional[list] = None,
    ) -> dict:
        return self._call(
            "mcp__HubSpot__search_crm_objects",
            objectType=object_type,
            filterGroups=filter_groups,
            properties=properties or [],
        )

    def manage_crm_objects(self, action: str, object_type: str, **kwargs) -> dict:
        return self._call(
            "mcp__HubSpot__manage_crm_objects",
            action=action,
            objectType=object_type,
            **kwargs,
        )


class MCPClient:
    """Unified MCP client with .gmail and .hubspot attributes."""

    def __init__(self, call_tool):
        self.gmail = _GmailMCP(call_tool)
        self.hubspot = _HubSpotMCP(call_tool)


def run_once(call_tool, since_hours: int = 1) -> list[SyncResult]:
    """
    Execute a single polling cycle.
    `call_tool` must be a callable: call_tool(tool_name, **kwargs) → dict
    Returns a list of SyncResult for every processed thread.
    """
    mcp = MCPClient(call_tool)
    threads = fetch_recent_threads(mcp, since_hours=since_hours)
    results = []
    for t in threads:
        r = process_thread(mcp, t["id"])
        if r:
            results.append(r)
            logger.info("%s", r)
    return results


def run_loop(call_tool, poll_seconds: int = 300) -> None:
    """Continuous loop version. Runs until the process is killed."""
    seen: set[str] = set()
    mcp = MCPClient(call_tool)
    logger.info("Sync loop started, polling every %ds", poll_seconds)
    while True:
        threads = fetch_recent_threads(mcp, since_hours=1)
        for t in threads:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            r = process_thread(mcp, t["id"])
            if r:
                logger.info("%s", r)
        time.sleep(poll_seconds)
