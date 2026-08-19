"""Webhook authenticity: X-Hub-Signature-256 and the subscription handshake.

Meta signs every webhook body with HMAC-SHA256 over the *raw* bytes. Re-encoding
the parsed JSON produces a different byte string and the check fails, so the
FastAPI handler passes `await request.body()` straight through to here.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_PREFIX = "sha256="


def sign(payload: bytes, app_secret: str) -> str:
    """Produce the header value Meta would send for this body."""
    digest = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def verify_signature(payload: bytes, header: str | None, app_secret: str) -> bool:
    """Constant-time check of the X-Hub-Signature-256 header.

    Returns False (never raises) for a missing header, a wrong prefix or a
    mismatch, so the caller can answer 403 uniformly.
    """
    if not app_secret or not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    return hmac.compare_digest(sign(payload, app_secret), header)


def verify_subscription(mode: str | None, token: str | None, expected_token: str) -> bool:
    """The one-time GET handshake Meta performs when you save the webhook URL."""
    return mode == "subscribe" and bool(expected_token) and token == expected_token
