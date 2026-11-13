"""System certificate injection, checked in fresh interpreters so the import order is real."""

import os
import subprocess
import sys

CHECK = """
import sys
{before}
import jumpstarter_cli
loaded = sorted(m for m in ("urllib3", "requests") if m in sys.modules)
import ssl, truststore, urllib3.util.ssl_
print(loaded, ssl.SSLContext is truststore.SSLContext, urllib3.util.ssl_.SSLContext is truststore.SSLContext)
"""


def _run(before: str = "") -> str:
    env = {**os.environ, "JUMPSTARTER_FORCE_SYSTEM_CERTS": "1"}
    code = CHECK.format(before=before)
    result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, env=env)
    return result.stdout.strip()


def test_injection_does_not_import_urllib3_or_requests():
    # urllib3, imported afterwards, still picks up truststore's SSLContext
    assert _run() == "[] True True"


def test_injection_patches_urllib3_imported_earlier():
    assert _run(before="import requests") == "['requests', 'urllib3'] True True"
