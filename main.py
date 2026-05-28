#!/usr/bin/env python3
"""Entry-point: Gmail → HubSpot contact sync daemon."""

from gmail_hubspot_sync import run_loop

if __name__ == "__main__":
    run_loop()
