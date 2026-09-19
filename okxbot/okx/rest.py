"""Synchronous OKX v5 REST client.

Scoped deliberately to SPOT. The 1h-1d horizon this bot targets makes latency
irrelevant, so this is plain blocking ``requests`` rather than async -- one
fewer source of concurrency bugs in the path that spends real money.
"""

from __future__ import annotations

import json
import logging
import random
import time
from urllib.parse import urlencode

import requests

from ..errors import OkxApiError, OkxHttpError
from .auth import Credentials, rest_headers
from .precision import InstrumentSpec

log = logging.getLogger(__name__)

LIVE_BASE = "https://www.okx.com"

# OKX signals "slow down" / "try again" through the envelope, not the status code.
RETRYABLE_CODES = {"50011", "50013", "50026"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Bars this bot is built around. OKX also exposes UTC-aligned variants ("1Dutc");
# we stay on the exchange-local day so candles line up with what the web UI shows.
SUPPORTED_BARS = ("1H", "2H", "4H", "6H", "12H", "1D", "1W")


class OkxRest:
    def __init__(
        self,
        creds: Credentials | None = None,
        base_url: str = LIVE_BASE,
        timeout: float = 15.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ):
        self.creds = creds
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self._instrument_cache: dict[str, InstrumentSpec] = {}

    # ---------------------------------------------------------------- plumbing

    def _request(self, method: str, path: str, params=None, body=None, auth=False):
        request_path = path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                request_path = f"{path}?{urlencode(clean)}"

        # The signature covers the exact body bytes, so serialise once and send
        # that same string -- never re-serialise via json=.
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""

        headers = {"Content-Type": "application/json"}
        if auth:
            if self.creds is None:
                raise OkxHttpError(f"{path} requires credentials but none were configured")
            headers = rest_headers(self.creds, method, request_path, payload)

        url = f"{self.base_url}{request_path}"
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            if attempt:
                # Full jitter: retries from several workers must not re-collide.
                delay = random.uniform(0, min(8.0, 0.5 * 2**attempt))
                log.warning("retry %d/%d for %s in %.2fs", attempt, self.max_retries, path, delay)
                time.sleep(delay)
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=headers,
                    data=payload if payload else None,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = OkxHttpError(f"{method} {path} failed: {exc}")
                continue

            if resp.status_code in RETRYABLE_STATUS:
                last_error = OkxHttpError(f"{method} {path} -> HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                raise OkxHttpError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:400]}")

            try:
                envelope = resp.json()
            except ValueError as exc:
                raise OkxHttpError(f"{method} {path} returned non-JSON: {resp.text[:200]}") from exc

            code = str(envelope.get("code", ""))
            if code == "0":
                return envelope.get("data", [])
            if code in RETRYABLE_CODES:
                last_error = OkxApiError(code, envelope.get("msg", ""), path, envelope.get("data"))
                continue
            raise OkxApiError(code, envelope.get("msg", ""), path, envelope.get("data"))

        raise last_error or OkxHttpError(f"{method} {path} exhausted retries")

    @staticmethod
    def _first(data, path: str) -> dict:
        if not data:
            raise OkxApiError("empty", "response carried no data", path)
        return data[0]

    @staticmethod
    def _check_scode(item: dict, path: str) -> dict:
        """Per-item status. A 200/code=0 envelope can still hold a rejected order."""
        scode = str(item.get("sCode", "0"))
        if scode not in ("0", ""):
            raise OkxApiError(scode, item.get("sMsg", ""), path, item)
        return item

    # ------------------------------------------------------------ market data

    def candles(self, inst_id: str, bar: str = "1H", limit: int = 300, after=None, before=None):
        """Recent candles, newest first.

        ``after``/``before`` are millisecond timestamps and page *backwards* and
        *forwards* respectively -- OKX's naming is the opposite of most APIs.
        """
        return self._request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": str(limit), "after": after, "before": before},
        )

    def history_candles(self, inst_id: str, bar: str = "1H", limit: int = 100, after=None, before=None):
        return self._request(
            "GET",
            "/api/v5/market/history-candles",
            params={"instId": inst_id, "bar": bar, "limit": str(limit), "after": after, "before": before},
        )

    def ticker(self, inst_id: str) -> dict:
        data = self._request("GET", "/api/v5/market/ticker", params={"instId": inst_id})
        return self._first(data, "/api/v5/market/ticker")

    def instrument(self, inst_id: str, refresh: bool = False) -> InstrumentSpec:
        if not refresh and inst_id in self._instrument_cache:
            return self._instrument_cache[inst_id]
        data = self._request(
            "GET", "/api/v5/public/instruments", params={"instType": "SPOT", "instId": inst_id}
        )
        spec = InstrumentSpec.from_api(self._first(data, "/api/v5/public/instruments"))
        self._instrument_cache[inst_id] = spec
        return spec

    # ---------------------------------------------------------------- account

    def balances(self, ccy: str | None = None) -> dict[str, dict]:
        """Map currency -> {avail, frozen, eq} for the trading account."""
        data = self._request("GET", "/api/v5/account/balance", params={"ccy": ccy}, auth=True)
        if not data:
            return {}
        out = {}
        for detail in data[0].get("details", []):
            out[detail["ccy"]] = {
                "avail": float(detail.get("availBal") or 0),
                "frozen": float(detail.get("frozenBal") or 0),
                "eq": float(detail.get("eq") or 0),
                "eq_usd": float(detail.get("eqUsd") or 0),
            }
        return out

    # ----------------------------------------------------------------- orders

    def place_order(
        self,
        inst_id: str,
        side: str,
        ord_type: str,
        sz: str,
        px: str | None = None,
        cl_ord_id: str | None = None,
        tgt_ccy: str | None = None,
    ) -> dict:
        """Place a single SPOT order (``tdMode`` is always ``cash``).

        ``tgt_ccy`` is passed explicitly for every market order: OKX defaults a
        SPOT market *buy* to interpreting ``sz`` as quote currency, which silently
        turns "buy 0.5 BTC" into "spend 0.5 USDT" if you forget.
        """
        body = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": ord_type,
            "sz": sz,
        }
        if px is not None:
            body["px"] = px
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        if tgt_ccy:
            body["tgtCcy"] = tgt_ccy
        data = self._request("POST", "/api/v5/trade/order", body=body, auth=True)
        return self._check_scode(self._first(data, "/api/v5/trade/order"), "/api/v5/trade/order")

    def place_oco(
        self,
        inst_id: str,
        side: str,
        sz: str,
        tp_trigger_px: str,
        sl_trigger_px: str,
        tp_ord_px: str = "-1",
        sl_ord_px: str = "-1",
        algo_cl_ord_id: str | None = None,
    ) -> dict:
        """One-cancels-the-other exit: whichever of TP/SL fires kills the sibling.

        ``-1`` as an order price means "execute at market once triggered", which
        is what you want for a stop -- a limit stop can be jumped clean over.
        """
        body = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": "oco",
            "sz": sz,
            "tpTriggerPx": tp_trigger_px,
            "tpOrdPx": tp_ord_px,
            "slTriggerPx": sl_trigger_px,
            "slOrdPx": sl_ord_px,
            "tpTriggerPxType": "last",
            "slTriggerPxType": "last",
        }
        if algo_cl_ord_id:
            body["algoClOrdId"] = algo_cl_ord_id
        data = self._request("POST", "/api/v5/trade/order-algo", body=body, auth=True)
        path = "/api/v5/trade/order-algo"
        return self._check_scode(self._first(data, path), path)

    def place_stop(
        self,
        inst_id: str,
        side: str,
        sz: str,
        sl_trigger_px: str,
        sl_ord_px: str = "-1",
        algo_cl_ord_id: str | None = None,
    ) -> dict:
        """Stop-loss with no take-profit leg.

        Used for the "runner" portion of a position whose take-profit fractions
        do not add up to the whole -- that remainder still needs a stop, or a
        plan with 70% of targets defined leaves 30% naked.
        """
        body = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": "conditional",
            "sz": sz,
            "slTriggerPx": sl_trigger_px,
            "slOrdPx": sl_ord_px,
            "slTriggerPxType": "last",
        }
        if algo_cl_ord_id:
            body["algoClOrdId"] = algo_cl_ord_id
        data = self._request("POST", "/api/v5/trade/order-algo", body=body, auth=True)
        path = "/api/v5/trade/order-algo"
        return self._check_scode(self._first(data, path), path)

    def order(self, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None) -> dict:
        data = self._request(
            "GET",
            "/api/v5/trade/order",
            params={"instId": inst_id, "ordId": ord_id, "clOrdId": cl_ord_id},
            auth=True,
        )
        return self._first(data, "/api/v5/trade/order")

    def pending_orders(self, inst_id: str | None = None) -> list[dict]:
        return self._request(
            "GET",
            "/api/v5/trade/orders-pending",
            params={"instType": "SPOT", "instId": inst_id},
            auth=True,
        )

    def pending_algos(self, inst_id: str | None = None, ord_type: str = "oco") -> list[dict]:
        return self._request(
            "GET",
            "/api/v5/trade/orders-algo-pending",
            params={"instType": "SPOT", "instId": inst_id, "ordType": ord_type},
            auth=True,
        )

    def cancel_order(self, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None) -> dict:
        body = {"instId": inst_id}
        if ord_id:
            body["ordId"] = ord_id
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        data = self._request("POST", "/api/v5/trade/cancel-order", body=body, auth=True)
        path = "/api/v5/trade/cancel-order"
        return self._check_scode(self._first(data, path), path)

    def cancel_algos(self, items: list[dict]) -> list[dict]:
        """``items`` are ``{"instId": ..., "algoId": ...}`` pairs."""
        if not items:
            return []
        return self._request("POST", "/api/v5/trade/cancel-algos", body=items, auth=True)

    def fills(self, inst_id: str | None = None, limit: int = 100) -> list[dict]:
        return self._request(
            "GET",
            "/api/v5/trade/fills",
            params={"instType": "SPOT", "instId": inst_id, "limit": str(limit)},
            auth=True,
        )
