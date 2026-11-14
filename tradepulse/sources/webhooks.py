"""Broker webhook adapters: verify -> normalize. The HTTP route only enqueues; it never sends anything.

Broker capabilities (what each broker actually pushes - no adapter pretends otherwise):

| Broker   | Push mechanism              | Authenticity                                      | Events |
|----------|-----------------------------|---------------------------------------------------|--------|
| Zerodha  | Kite Connect postback (JSON)| SHA-256(order_id + order_timestamp + api_secret)   | OPEN, UPDATE (modify / partial fill), COMPLETE, CANCELLED, REJECTED |
| Angel One| SmartAPI order postback     | none from Angel -> shared token in the URL        | open, modified, complete, cancelled, rejected, (trigger) pending |
| 5paisa   | none for order updates      | -                                                 | NOT SUPPORTED - covered by the OMS stream |
| generic  | TradePulse schema           | HMAC-SHA256(body) in ``X-TradePulse-Signature``    | any normalized type (for other systems / testing) |

A Kite app has exactly one postback URL. If the OMS's app already uses it, point it at TradePulse
only if nothing else needs it; the OMS stream covers the same lifecycle either way.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from .. import events as ev


class WebhookRejected(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _float(v):
    try:
        f = float(v)
        return f if f else None
    except (TypeError, ValueError):
        return None


class Adapter:
    name = ""
    supported = True
    events: tuple = ()
    auth = ""

    def __init__(self, oms, vault, settings):
        self.oms, self.vault, self.settings = oms, vault, settings

    def handle(self, raw: bytes, headers: dict, query: dict) -> list[dict]:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"broker": self.name, "supported": self.supported, "events": list(self.events), "auth": self.auth,
                "secret_set": bool(self.vault.get(f"webhook:{self.name}")) if self.supported else False}

    @staticmethod
    def _json(raw: bytes) -> dict:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise WebhookRejected(400, "body is not JSON")
        if not isinstance(data, dict):
            raise WebhookRejected(400, "body must be a JSON object")
        return data


class ZerodhaAdapter(Adapter):
    name = "zerodha"
    events = ("ORDER_OPEN", "ORDER_MODIFIED", "ORDER_PARTIALLY_FILLED", "ORDER_FILLED", "ORDER_CANCELLED",
              "ORDER_REJECTED")
    auth = "Kite checksum: SHA-256(order_id + order_timestamp + api_secret)"

    def _secret(self, account_id: str) -> str:
        # The Kite API secret is configured in TradePulse (encrypted). TradePulse never reads it from the OMS.
        return self.vault.get(f"webhook:zerodha:{account_id}") or self.vault.get("webhook:zerodha")

    def handle(self, raw, headers, query):
        d = self._json(raw)
        order_id = str(d.get("order_id") or "")
        if not order_id:
            raise WebhookRejected(400, "missing order_id")
        account_id = self.oms.account_for_broker_client("zerodha", str(d.get("user_id") or "")) \
            or self.oms.account_by_broker_order_id("zerodha", order_id)
        if not account_id:
            raise WebhookRejected(404, "account not found for this postback")
        secret = self._secret(account_id)
        if not secret:
            raise WebhookRejected(401, "no API secret available to verify the checksum")
        expected = hashlib.sha256(f"{order_id}{d.get('order_timestamp', '')}{secret}".encode()).hexdigest()
        if not hmac.compare_digest(expected, str(d.get("checksum") or "")):
            raise WebhookRejected(401, "checksum mismatch")
        status = str(d.get("status") or "").upper()
        filled, pending = _int(d.get("filled_quantity")), _int(d.get("pending_quantity"))
        if status == "COMPLETE":
            et = ev.ORDER_FILLED
        elif status == "REJECTED":
            et = ev.ORDER_REJECTED
        elif status == "CANCELLED":
            et = ev.ORDER_CANCELLED
        elif status in ("OPEN", "UPDATE"):
            et = ev.ORDER_PARTIALLY_FILLED if filled and pending else (ev.ORDER_OPEN if status == "OPEN" else ev.ORDER_MODIFIED)
        else:
            et = ev.ORDER_OPEN if "PENDING" in status or "RECEIVED" in status else None
        if et is None:
            return []
        key = ev.order_key(account_id, order_id, et, filled if et in (ev.ORDER_FILLED, ev.ORDER_PARTIALLY_FILLED) else 0)
        if et == ev.ORDER_MODIFIED:
            key += ":" + ev.stable_hash(d.get("exchange_update_timestamp"), d.get("price"), d.get("quantity"))
        return [ev.new_event(
            et, "WEBHOOK:zerodha", key, account_id=account_id, broker="zerodha", symbol=d.get("tradingsymbol") or "",
            exchange=d.get("exchange") or "", side=d.get("transaction_type") or "", order_type=d.get("order_type") or "",
            product=d.get("product") or "", quantity=_int(d.get("quantity")), filled_quantity=filled,
            price=_float(d.get("price")), average_price=_float(d.get("average_price")), status=status,
            order_id=order_id, message=str(d.get("status_message") or "")[:500],
            payload={k: d.get(k) for k in ("exchange_order_id", "tag", "variety", "pending_quantity",
                                            "cancelled_quantity", "exchange_update_timestamp")},
            occurred_at=_iso(d.get("exchange_update_timestamp") or d.get("order_timestamp")))]


class AngelAdapter(Adapter):
    name = "angel"
    events = ("ORDER_OPEN", "ORDER_MODIFIED", "ORDER_PARTIALLY_FILLED", "ORDER_FILLED", "ORDER_CANCELLED",
              "ORDER_REJECTED")
    auth = "Shared token in the postback URL (?token=...) - Angel does not sign postbacks"

    def handle(self, raw, headers, query):
        want = self.vault.get("webhook:angel")
        if not want:
            raise WebhookRejected(401, "Angel webhook token not configured in TradePulse")
        if not hmac.compare_digest(str(query.get("token") or ""), want):
            raise WebhookRejected(401, "bad token")
        d = self._json(raw)
        order_id = str(d.get("orderid") or d.get("order_id") or "")
        if not order_id:
            raise WebhookRejected(400, "missing orderid")
        account_id = self.oms.account_for_broker_client("angel", str(d.get("clientcode") or query.get("account") or "")) \
            or self.oms.account_by_broker_order_id("angel", order_id)
        if not account_id:
            raise WebhookRejected(404, "account not found for this postback")
        status = str(d.get("orderstatus") or d.get("status") or "").lower()
        filled, unfilled = _int(d.get("filledshares")), _int(d.get("unfilledshares"))
        et = {"complete": ev.ORDER_FILLED, "rejected": ev.ORDER_REJECTED, "cancelled": ev.ORDER_CANCELLED,
              "modified": ev.ORDER_MODIFIED}.get(status)
        if et is None and status in ("open", "open pending", "trigger pending", "validation pending",
                                     "after market order req received", "put order req received"):
            et = ev.ORDER_PARTIALLY_FILLED if filled and unfilled else ev.ORDER_OPEN
        if et is None:
            return []
        key = ev.order_key(account_id, order_id, et, filled if et in (ev.ORDER_FILLED, ev.ORDER_PARTIALLY_FILLED) else 0)
        if et == ev.ORDER_MODIFIED:
            key += ":" + ev.stable_hash(d.get("updatetime"), d.get("price"), d.get("quantity"))
        return [ev.new_event(
            et, "WEBHOOK:angel", key, account_id=account_id, broker="angel", symbol=d.get("tradingsymbol") or "",
            exchange=d.get("exchange") or "", side=str(d.get("transactiontype") or "").upper(),
            order_type=d.get("ordertype") or "", product=d.get("producttype") or "", quantity=_int(d.get("quantity")),
            filled_quantity=filled, price=_float(d.get("price")), average_price=_float(d.get("averageprice")),
            status=status.upper(), order_id=order_id, message=str(d.get("text") or "")[:500],
            payload={k: d.get(k) for k in ("variety", "updatetime", "exchorderupdatetime", "unfilledshares")})]


class FivePaisaAdapter(Adapter):
    name = "5paisa"
    supported = False
    auth = "-"

    def handle(self, raw, headers, query):
        raise WebhookRejected(501, "5paisa does not push order updates to a webhook. 5paisa accounts are "
                                   "monitored through the OMS event stream instead.")


class GenericAdapter(Adapter):
    name = "generic"
    events = tuple(ev.EVENT_TYPES)
    auth = "HMAC-SHA256 of the raw body, hex, in X-TradePulse-Signature"

    def handle(self, raw, headers, query):
        secret = self.vault.get("webhook:generic")
        if not secret:
            raise WebhookRejected(401, "generic webhook secret not configured")
        sig = headers.get("x-tradepulse-signature", "")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise WebhookRejected(401, "bad signature")
        d = self._json(raw)
        et = str(d.get("event_type") or "").upper()
        if et not in ev.EVENT_TYPES:
            raise WebhookRejected(400, f"unknown event_type {et!r}")
        account_id = str(d.get("account_id") or "")
        if not account_id or not self.oms.account(account_id):
            raise WebhookRejected(404, "unknown account_id")
        event_ref = str(d.get("event_id") or "")
        order_id = str(d.get("order_id") or "")
        if order_id and et.startswith("ORDER_"):
            key = ev.order_key(account_id, order_id, et, _int(d.get("filled_quantity")))
        elif event_ref:
            key = f"generic:{event_ref}"
        else:
            raise WebhookRejected(400, "event_id or order_id is required for idempotency")
        return [ev.new_event(
            et, "WEBHOOK:generic", key, account_id=account_id, broker=str(d.get("broker") or ""),
            symbol=str(d.get("symbol") or ""), exchange=str(d.get("exchange") or ""), side=str(d.get("side") or "").upper(),
            order_type=str(d.get("order_type") or ""), quantity=_int(d.get("quantity")),
            filled_quantity=_int(d.get("filled_quantity")), price=_float(d.get("price")),
            average_price=_float(d.get("average_price")), status=str(d.get("status") or ""), order_id=order_id,
            trade_id=str(d.get("trade_id") or ""), message=str(d.get("message") or "")[:500],
            trading_mode=str(d.get("trading_mode") or ""), occurred_at=d.get("occurred_at") or None)]


def _iso(ts) -> str | None:
    """Kite timestamps are 'YYYY-MM-DD HH:MM:SS' in IST."""
    if not ts:
        return None
    s = str(ts).strip().replace(" ", "T")
    return s if "+" in s else s + "+05:30"


def adapters(oms, vault, settings) -> dict[str, Adapter]:
    return {a.name: a(oms, vault, settings) for a in (ZerodhaAdapter, AngelAdapter, FivePaisaAdapter, GenericAdapter)}
