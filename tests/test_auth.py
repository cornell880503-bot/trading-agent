import base64
import hashlib
import hmac
from datetime import datetime, timezone

from okxbot.okx.auth import Credentials, rest_headers, rest_timestamp, sign, ws_login_args

CREDS = Credentials(api_key="key-1", api_secret="secret-1", passphrase="pass-1", simulated=True)


def test_signature_matches_the_documented_construction():
    ts = "2026-09-19T08:00:00.123Z"
    got = sign(ts, "GET", "/api/v5/account/balance", "", "secret-1")
    expected = base64.b64encode(
        hmac.new(
            b"secret-1",
            f"{ts}GET/api/v5/account/balance".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert got == expected


def test_body_is_part_of_the_signed_string():
    ts = "2026-09-19T08:00:00.123Z"
    a = sign(ts, "POST", "/api/v5/trade/order", '{"sz":"1"}', "s")
    b = sign(ts, "POST", "/api/v5/trade/order", '{"sz":"2"}', "s")
    assert a != b, "a different body must produce a different signature"


def test_method_is_uppercased_before_signing():
    ts = "2026-09-19T08:00:00.123Z"
    assert sign(ts, "get", "/p", "", "s") == sign(ts, "GET", "/p", "", "s")


def test_rest_timestamp_has_exactly_millisecond_precision():
    ts = rest_timestamp(datetime(2026, 9, 19, 8, 0, 0, 123456, tzinfo=timezone.utc))
    assert ts == "2026-09-19T08:00:00.123Z"


def test_demo_flag_is_only_present_when_simulated():
    demo = rest_headers(CREDS, "GET", "/api/v5/account/balance")
    live = rest_headers(
        Credentials("k", "s", "p", simulated=False), "GET", "/api/v5/account/balance"
    )
    assert demo["x-simulated-trading"] == "1"
    assert "x-simulated-trading" not in live


def test_ws_login_uses_epoch_seconds_not_iso():
    args = ws_login_args(CREDS, now=1789000000.0)
    assert args["timestamp"] == "1789000000"
    assert args["sign"] == sign("1789000000", "GET", "/users/self/verify", "", "secret-1")


def test_credentials_never_leak_through_repr():
    text = f"{CREDS!r} {CREDS}"
    assert "secret-1" not in text and "pass-1" not in text
    assert "ey-1" in text  # the last four of the key are fine to show
