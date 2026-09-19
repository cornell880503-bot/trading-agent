"""OKX v5 request signing.

The signature is::

    Base64( HMAC-SHA256( timestamp + METHOD + requestPath + body , secret ) )

``requestPath`` includes the query string and ``body`` is the raw JSON string
exactly as sent (empty string for GET). Any mismatch between the signed string
and the bytes actually transmitted yields ``50113 Invalid Sign``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import datetime, timezone

VERIFY_PATH = "/users/self/verify"


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str
    passphrase: str
    simulated: bool = True

    def redacted(self) -> str:
        tail = self.api_key[-4:] if len(self.api_key) >= 4 else "?"
        env = "demo" if self.simulated else "LIVE"
        return f"<OKX key ...{tail} ({env})>"

    def __repr__(self) -> str:  # never let a secret reach a log or traceback
        return self.redacted()


def rest_timestamp(now: datetime | None = None) -> str:
    """ISO-8601 with exactly millisecond precision, as REST requires."""
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{now.microsecond // 1000:03d}Z"


def ws_timestamp(now: float | None = None) -> str:
    """Epoch seconds. The WebSocket login op wants this form, not ISO-8601."""
    return str(int(now if now is not None else time.time()))


def sign(timestamp: str, method: str, request_path: str, body: str, secret: str) -> str:
    message = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def rest_headers(
    creds: Credentials,
    method: str,
    request_path: str,
    body: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    ts = rest_timestamp(now)
    headers = {
        "OK-ACCESS-KEY": creds.api_key,
        "OK-ACCESS-SIGN": sign(ts, method, request_path, body, creds.api_secret),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": creds.passphrase,
        "Content-Type": "application/json",
    }
    if creds.simulated:
        headers["x-simulated-trading"] = "1"
    return headers


def ws_login_args(creds: Credentials, now: float | None = None) -> dict[str, str]:
    """Payload for ``{"op": "login", "args": [<this>]}`` on the private socket."""
    ts = ws_timestamp(now)
    return {
        "apiKey": creds.api_key,
        "passphrase": creds.passphrase,
        "timestamp": ts,
        "sign": sign(ts, "GET", VERIFY_PATH, "", creds.api_secret),
    }
