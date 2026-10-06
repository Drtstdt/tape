"""HTTPS context for urllib.

Python's stdlib on Windows verifies TLS against the Windows certificate store,
where an outdated root (e.g. an expired cross-signed root still preferred by an
old store) makes valid sites fail with "certificate has expired". The certifi
bundle (installed with requests/httpx in this venv) is current, so it is
preferred when present; the system store is the fallback.
"""

from __future__ import annotations

import ssl


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def ca_source() -> str:
    try:
        import certifi
        return f"certifi {certifi.where()}"
    except ImportError:
        return "system store (certifi not installed)"
