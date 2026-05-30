#!/usr/bin/env python3
"""
Run this script once to complete Google OAuth2 authorization.
It will open a browser window, ask you to log in, and save the token locally.
"""
from gmail_hubspot_sync.gmail_client import GmailClient

print("Avvio autenticazione Google OAuth2 …")
GmailClient()
print("✓ Token salvato. Ora puoi eseguire: python main.py")
