"""
Gmail → HubSpot Sync — MCP-native runner
=========================================
This module replaces the thin shims in gmail_hubspot_sync.py with real
calls to the Gmail MCP and HubSpot MCP tools that are available in the
Claude Code / MCP execution environment.

Usage (from a Claude Code session or MCP-aware runtime):

    import gmail_hubspot_mcp
    gmail_hubspot_mcp.run()          # one-shot
    gmail_hubspot_mcp.run(watch=60)  # poll every 60 s

The module monkey-patches the `gmail` and `hubspot` pseudo-modules used
by gmail_hubspot_sync so no other file needs changing.
"""

from __future__ import annotations

import logging
import time
import types
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thin adapter objects that delegate to the MCP tool functions injected by
# the runtime.  Each method signature mirrors what gmail_hubspot_sync.py
# expects, so the core logic stays untouched.
# ---------------------------------------------------------------------------


class GmailAdapter:
    """Wraps Gmail MCP tool calls."""

    def __init__(self, mcp_search, mcp_get_thread):
        self._search = mcp_search
        self._get_thread = mcp_get_thread

    def search_threads(self, query: str = "in:inbox", maxResults: int = 50) -> dict:
        return self._search(query=query, maxResults=maxResults)

    def get_thread(self, threadId: str) -> dict:
        return self._get_thread(thread_id=threadId)


class HubSpotAdapter:
    """Wraps HubSpot MCP tool calls."""

    def __init__(self, mcp_search, mcp_manage):
        self._search = mcp_search
        self._manage = mcp_manage

    def search_crm_objects(self, **kwargs) -> dict:
        return self._search(**kwargs)

    def manage_crm_objects(self, **kwargs) -> dict:
        return self._manage(**kwargs)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(
    gmail_search_fn: Any,
    gmail_get_thread_fn: Any,
    hubspot_search_fn: Any,
    hubspot_manage_fn: Any,
    watch: int = 0,
    max_results: int = 50,
) -> None:
    """
    Wire up the MCP functions, patch the shim modules, then delegate to the
    core sync loop.

    Parameters
    ----------
    gmail_search_fn      : callable – mcp__Gmail__search_threads bound function
    gmail_get_thread_fn  : callable – mcp__Gmail__get_thread bound function
    hubspot_search_fn    : callable – mcp__HubSpot__search_crm_objects bound function
    hubspot_manage_fn    : callable – mcp__HubSpot__manage_crm_objects bound function
    watch                : int – polling interval in seconds; 0 = one-shot
    max_results          : int – max Gmail threads per scan
    """
    import gmail_hubspot_sync as _sync

    # Inject real adapters into the sync module
    _sync.gmail = GmailAdapter(gmail_search_fn, gmail_get_thread_fn)
    _sync.hubspot = HubSpotAdapter(hubspot_search_fn, hubspot_manage_fn)

    if watch > 0:
        log.info("Watch mode: scanning every %d s. Ctrl-C to stop.", watch)
        try:
            while True:
                results = _sync.run_sync(max_results=max_results)
                _sync._print_summary(results)
                time.sleep(watch)
        except KeyboardInterrupt:
            log.info("Stopped.")
    else:
        results = _sync.run_sync(max_results=max_results)
        _sync._print_summary(results)
