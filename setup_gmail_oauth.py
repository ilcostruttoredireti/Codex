#!/usr/bin/env python3
"""
One-time helper: run this to authorise Gmail OAuth and save the token.
You only need to run it once; main.py reuses the saved token automatically.

Usage:
    python setup_gmail_oauth.py
"""

from dotenv import load_dotenv
import os

load_dotenv()

from gmail_monitor import GmailMonitor

credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
state_file = os.getenv("STATE_FILE", ".gmail_state.json")

print("Opening browser for Gmail OAuth…")
# Instantiating GmailMonitor triggers OAuth if token.json is missing
monitor = GmailMonitor(credentials_file, token_file, state_file)
print(f"✓ Token saved to {token_file}")
print("You can now run: python main.py")
