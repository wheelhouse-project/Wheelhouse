"""A structurally complete Google service-account key for tests.

The credentials picker validates the picked file with Google's own
credential loader (review finding wh-google-creds-file-picker.1.4), so
a fixture that carries only ``type`` and ``project_id`` is no longer a
valid key. This module builds a key with every field the loader
requires, including a real (freshly generated, test-only) RSA private
key. The key pair is generated once per process and cached; it signs
nothing and never leaves the test run.
"""

from __future__ import annotations

import json

_cached_pem: str | None = None


def _private_key_pem() -> str:
    global _cached_pem
    if _cached_pem is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _cached_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
    return _cached_pem


def service_account_key_dict(**overrides) -> dict:
    """A dict with every field Google's credential loader requires.

    Keyword overrides replace fields, so a test can corrupt exactly one
    field (for example ``private_key="not a key"``) and know the rest of
    the key is valid.
    """
    data = {
        "type": "service_account",
        "project_id": "demo",
        "private_key_id": "0123456789abcdef0123456789abcdef01234567",
        "private_key": _private_key_pem(),
        "client_email": "demo@demo.iam.gserviceaccount.com",
        "client_id": "123456789012345678901",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    data.update(overrides)
    return data


def write_service_account_key(path, **overrides):
    """Write a complete key file at ``path`` and return the path."""
    path.write_text(
        json.dumps(service_account_key_dict(**overrides)), encoding="utf-8"
    )
    return path
