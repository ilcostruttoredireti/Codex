"""
Stub out modules whose native extensions are unavailable in the test
environment (google-auth → cryptography → cffi).  Registering mocks in
sys.modules before any test module is imported means that when sync.py
does `import gmail_client`, it receives the stub instead of the real
module, keeping unit tests fast and hermetic.
"""
import sys
from unittest.mock import MagicMock

for _mod in ("gmail_client", "hubspot_client"):
    sys.modules.setdefault(_mod, MagicMock())
