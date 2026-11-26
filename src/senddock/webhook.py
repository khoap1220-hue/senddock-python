from __future__ import annotations

import hashlib
import hmac
import math
import time

WEBHOOK_SIGNATURE_HEADER = "X-SendDock-Signature"
DEFAULT_TOLERANCE_SECONDS = 300


def _parse_unix_timestamp(raw: str) -> int | None:
    if not raw:
        return None
    digits = raw[1:] if raw.startswith("-") else raw
    if not digits or not digits.isascii() or not digits.isdigit():
        return None
    return int(raw)


def verify_webhook_signature(
    payload: str | bytes | bytearray | memoryview,
    signature: str,
    secret: str,
    *,
    tolerance_seconds: int | float = DEFAULT_TOLERANCE_SECONDS,
    now: int | float | None = None,
) -> bool:
    """Verify an incoming ``X-SendDock-Signature: t=<unix>,v1=<hmac>`` header.

    Uses HMAC-SHA256 over ``f"{timestamp}." + raw_payload``, enforces a replay
    tolerance window (300s by default), and compares digests in constant time.
    """
    if not isinstance(signature, str) or not signature:
        return False
    if not isinstance(secret, str) or not secret:
        return False
    if not math.isfinite(tolerance_seconds) or tolerance_seconds < 0:
        return False

    effective_now = math.floor(time.time()) if now is None else now
    if not math.isfinite(effective_now):
        return False

    timestamp: int | None = None
    v1: str | None = None
    for part in signature.split(","):
        eq = part.find("=")
        if eq == -1:
            continue
        key = part[:eq].strip()
        value = part[eq + 1 :].strip()
        if key == "t":
            timestamp = _parse_unix_timestamp(value)
        elif key == "v1":
            v1 = value

    if timestamp is None or v1 is None:
        return False
    if abs(effective_now - timestamp) > tolerance_seconds:
        return False
    if not v1 or any(ch.isspace() for ch in v1):
        return False

    try:
        received = bytes.fromhex(v1)
    except ValueError:
        return False

    raw_payload = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(f"{timestamp}.".encode("ascii"))
    mac.update(raw_payload)
    expected = mac.digest()

    if len(received) != len(expected):
        return False
    return hmac.compare_digest(expected, received)
