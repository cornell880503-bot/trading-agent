"""Transport-level guarantees, exercised without a network."""

import json

import pytest

from okxbot.errors import OkxApiError, OkxHttpError, ReadOnlyViolation
from okxbot.okx.auth import Credentials
from okxbot.okx.rest import OkxRest

CREDS = Credentials("key", "secret", "pass", simulated=True)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Records requests and replays a queue of canned responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data})
        return self.responses.pop(0) if self.responses else FakeResponse({"code": "0", "data": []})


def client(read_only=False, *responses):
    return OkxRest(creds=CREDS, session=FakeSession(*responses), read_only=read_only,
                   max_retries=0)


# --------------------------------------------------------------- read-only


def test_a_read_only_client_refuses_to_place_an_order():
    rest = client(True)
    with pytest.raises(ReadOnlyViolation, match="refusing POST"):
        rest.place_order("BTC-USDT", "buy", "limit", "1", px="100")


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.place_order("BTC-USDT", "buy", "market", "1"),
        lambda r: r.place_oco("BTC-USDT", "sell", "1", "110", "90"),
        lambda r: r.place_stop("BTC-USDT", "sell", "1", "90"),
        lambda r: r.cancel_order("BTC-USDT", ord_id="1"),
        lambda r: r.cancel_algos([{"instId": "BTC-USDT", "algoId": "1"}]),
    ],
)
def test_every_write_path_is_blocked_not_just_the_obvious_one(call):
    rest = client(True)
    with pytest.raises(ReadOnlyViolation):
        call(rest)


def test_a_read_only_client_never_touches_the_network_to_refuse():
    rest = client(True)
    with pytest.raises(ReadOnlyViolation):
        rest.place_order("BTC-USDT", "buy", "market", "1")
    assert rest.session.calls == [], "the refusal must precede the request"


def test_reads_still_work_while_read_only():
    rest = client(True, FakeResponse({"code": "0", "data": [{"last": "100"}]}))
    assert rest.ticker("BTC-USDT")["last"] == "100"


def test_writes_work_when_not_read_only():
    rest = client(False, FakeResponse({"code": "0", "data": [{"ordId": "1", "sCode": "0"}]}))
    assert rest.place_order("BTC-USDT", "buy", "market", "1")["ordId"] == "1"


# ------------------------------------------------------------- the envelope


def test_a_nonzero_envelope_code_raises_even_on_http_200():
    rest = client(False, FakeResponse({"code": "50119", "msg": "API key doesn't exist"}))
    with pytest.raises(OkxApiError) as caught:
        rest.balances()
    assert caught.value.code == "50119"
    assert "base_url" in str(caught.value), "the hint must reach the operator"


def test_a_rejected_order_inside_a_successful_envelope_still_raises():
    # code=0 at the envelope, sCode!=0 on the item: the order did not happen.
    rest = client(False, FakeResponse({"code": "0", "data": [{"sCode": "51008", "sMsg": "no funds"}]}))
    with pytest.raises(OkxApiError) as caught:
        rest.place_order("BTC-USDT", "buy", "market", "1")
    assert caught.value.code == "51008"


def test_the_signed_body_is_the_bytes_actually_sent():
    """Re-serialising between signing and sending is how 50113 happens."""
    rest = client(False, FakeResponse({"code": "0", "data": [{"ordId": "1", "sCode": "0"}]}))
    rest.place_order("BTC-USDT", "buy", "limit", "1", px="100", cl_ord_id="abc")
    sent = rest.session.calls[0]["data"]
    assert json.loads(sent)["clOrdId"] == "abc"
    assert " " not in sent, "compact separators keep the signed string stable"


def test_the_demo_header_rides_on_authenticated_requests():
    rest = client(False, FakeResponse({"code": "0", "data": []}))
    rest.balances()
    assert rest.session.calls[0]["headers"]["x-simulated-trading"] == "1"


def test_public_endpoints_are_not_signed():
    rest = client(False, FakeResponse({"code": "0", "data": [{"last": "1"}]}))
    rest.ticker("BTC-USDT")
    assert "OK-ACCESS-SIGN" not in rest.session.calls[0]["headers"]


def test_an_authenticated_call_without_credentials_is_refused():
    rest = OkxRest(creds=None, session=FakeSession(), max_retries=0)
    with pytest.raises(OkxHttpError, match="requires credentials"):
        rest.balances()
