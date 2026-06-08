"""Stub out the Google API packages so tests run without native crypto libs."""
import sys
from unittest.mock import MagicMock

# These modules require native extensions unavailable in CI
for mod in [
    "google",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google.oauth2",
    "google.oauth2.credentials",
    "google_auth_oauthlib",
    "google_auth_oauthlib.flow",
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.errors",
]:
    sys.modules.setdefault(mod, MagicMock())

# Provide the specific symbols the app imports
sys.modules["google.auth.transport.requests"].Request = MagicMock
sys.modules["google.oauth2.credentials"].Credentials = MagicMock
sys.modules["google_auth_oauthlib.flow"].InstalledAppFlow = MagicMock
sys.modules["googleapiclient.errors"].HttpError = type("HttpError", (Exception,), {
    "resp": type("R", (), {"status": 200})()
})
