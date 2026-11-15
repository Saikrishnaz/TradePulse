"""TradePulse's next-day signal intake. Same payload, answers and Redis store as the OMS's
``POST /api/v1/signals/next-day``; only the inputs differ, because TradePulse cannot read the OMS:

* **Auth** - the OMS's own algo keys, published to ``smx:presig:algokeys`` with each secret encrypted
  under ``SMX_PRESIG_KEY`` (a Fernet key both hosts hold). Same headers, ±30 s window and HMAC as the
  OMS webhook; nonces are shared in Redis, so a request sent to both servers is taken once.
* **Validation** - against the OMS-published strategy template and instrument master: the next-day
  rules (window, entries only, PREMARKET = LIMIT with a price), the strategy (exists, enabled, legs),
  every order's shape, and its instrument (known, tradable, not expired on the trading date, right
  category / option type / exchange / underlying, price on the tick). The OMS validates everything
  again at the morning pickup, with the full live-webhook rules, and is the authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

from . import contract
from . import keys as rk
from .store import PresignalStore, StoreRefused, StoreUnavailable, payload_sha256

log = logging.getLogger("tradepulse.presignal")

MAX_BODY_BYTES = 256 * 1024
MAX_SKEW_S = 30
NONCE_TTL_S = 3600
NONCE_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")
SIGNAL_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{6,64}$")
STRATEGY_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,64}$")
LEG_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
EXCHANGES = ("NSE", "BSE", "NFO", "BFO")
SIDES = {"BUY": "OPEN", "SHORT": "OPEN", "SELL": "CLOSE", "COVER": "CLOSE"}
PRODUCTS = ("MIS", "NRML", "CNC", "MTF")
ORDER_TYPES = ("LIMIT", "LIMITMARKET", "MARKET", "SL_LIMIT")
CATEGORY_CLASSES = {"OPTIONS": {"CE", "PE"}, "FUTURES": {"FUT"}, "EQUITY": {"EQ"}}
CATEGORY_PRODUCTS = {"OPTIONS": {"MIS", "NRML"}, "FUTURES": {"MIS", "NRML"}, "EQUITY": {"MIS", "CNC", "MTF"}}
CLASS_TEMPLATE = {"EQ": "EQ", "CE": "OPT", "PE": "OPT", "FUT": "FUT"}
MAX_LEG_ORDERS, MAX_BASKET_ORDERS = 20, 200


class Refused(Exception):
    def __init__(self, code: str, message: str, http_status: int = 422, errors: list | None = None):
        super().__init__(message)
        self.code, self.message, self.http_status, self.errors = code, message, http_status, errors or []


def sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """The OMS's webhook signature (backend/oms/webhook_auth.sign)."""
    return hmac.new(secret.encode(), f"{timestamp}\n{nonce}\n".encode() + body, hashlib.sha256).hexdigest()


def _decimal(v):
    if isinstance(v, bool) or v in (None, ""):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


class Intake:
    def __init__(self, client_getter, presig_key: str, clock=None):
        """``client_getter()`` -> a redis-py client (None when Redis is not available)."""
        self.client_getter = client_getter
        self.presig_key = presig_key
        self.clock = clock or (lambda: datetime.now(contract.IST))

    # --- entry point -------------------------------------------------
    def handle(self, body: bytes, headers: dict, source_ip: str = "") -> tuple[int, dict]:
        if len(body) > MAX_BODY_BYTES:
            return 413, {"error": "PAYLOAD_TOO_LARGE"}
        client = self.client_getter()
        if client is None:
            return 503, self._unavailable("")
        store = PresignalStore(client)
        try:
            key = self._authenticate(client, body, {k.lower(): v for k, v in headers.items()})
        except Refused as e:
            return e.http_status, {"error": e.code}
        except StoreUnavailable:
            return 503, self._unavailable("")
        except Exception as e:
            if type(e).__module__.startswith("redis"):
                return 503, self._unavailable("")
            raise
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, {"error": "INVALID_JSON"}
        sid = payload.get("signal_id") if isinstance(payload, dict) and isinstance(payload.get("signal_id"), str) else ""
        strategy_id = payload.get("strategy_id") if isinstance(payload, dict) and isinstance(payload.get("strategy_id"), str) else ""
        try:
            snap = self._validate(client, payload, key, source_ip)
            outcome, version = store.put(snap)
        except (Refused, contract.ContractError) as e:
            log.info("next-day signal %s refused: %s %s", sid, e.code, e.message)
            return e.http_status, {"signal_id": sid, "strategy_id": strategy_id, "status": "REJECTED", "code": e.code,
                                   "message": e.message, "errors": e.errors}
        except StoreRefused as e:
            log.info("next-day signal %s refused: %s %s", sid, e.code, e.message)
            return e.http_status, {"signal_id": sid, "strategy_id": strategy_id, "status": "REJECTED", "code": e.code,
                                   "message": e.message, "errors": []}
        except StoreUnavailable:
            return 503, self._unavailable(sid)
        except Exception as e:
            if type(e).__module__.startswith("redis"):
                return 503, self._unavailable(sid)
            raise
        log.info("next-day signal %s %s v%s for %s", sid, outcome, version, snap["trading_date"])
        out = {"signal_id": sid, "strategy_id": strategy_id, "status": outcome, "version": version,
               "trading_date": snap["trading_date"], "policy": snap["policy"]}
        if outcome == "DUPLICATE":
            out["duplicate"] = True
        return (202 if outcome == "STORED" else 200), out

    @staticmethod
    def _unavailable(sid: str) -> dict:
        return {"signal_id": sid, "status": "NOT_STORED", "code": "STORE_UNAVAILABLE",
                "message": "the signal store is unreachable; nothing was stored - resend the same signal"}

    # --- auth (mirrors backend/oms/webhook_auth.authenticate) ---------
    def _authenticate(self, client, body: bytes, h: dict) -> dict:
        bearer_key, bearer_secret = "", ""
        auth = h.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if "." not in token:
                raise Refused("MALFORMED_BEARER", "", 401)
            bearer_key, bearer_secret = token.split(".", 1)
        key_id = bearer_key or h.get("x-smx-key-id", "")
        if not KEY_ID_RE.match(key_id or ""):
            raise Refused("MISSING_CREDENTIALS", "", 401)
        if bearer_key and h.get("x-smx-key-id") and h["x-smx-key-id"] != bearer_key:
            raise Refused("KEY_ID_MISMATCH", "", 401)
        ts, nonce = h.get("x-smx-timestamp", ""), h.get("x-smx-nonce", "")
        if not ts.isdigit():
            raise Refused("MISSING_TIMESTAMP", "", 401)
        if abs(time.time() - int(ts)) > MAX_SKEW_S:
            raise Refused("TIMESTAMP_OUT_OF_WINDOW", "", 401)
        if not NONCE_RE.match(nonce):
            raise Refused("MISSING_NONCE", "", 401)
        raw = client.get(rk.PRESIG_ALGO_KEYS)
        keys = json.loads(raw) if raw else {}
        row = keys.get(key_id)
        if not row or not row.get("enabled") or not self.presig_key:
            raise Refused("INVALID_CREDENTIALS", "", 401)
        try:
            from cryptography.fernet import Fernet

            secret = Fernet(self.presig_key.encode()).decrypt(row["secret_enc"].encode()).decode()
        except Exception:
            log.error("algo key %s could not be decrypted: SMX_PRESIG_KEY differs from the OMS's", key_id)
            raise Refused("INVALID_CREDENTIALS", "", 401)
        mode = row.get("auth_mode") or "BEARER_HMAC"
        if mode in ("BEARER", "BEARER_HMAC") and (not bearer_secret or not hmac.compare_digest(bearer_secret, secret)):
            raise Refused("INVALID_CREDENTIALS", "", 401)
        if mode in ("HMAC", "BEARER_HMAC"):
            given = h.get("x-smx-signature", "").lower()
            if not given or not hmac.compare_digest(given, sign(secret, ts, nonce, body)):
                raise Refused("INVALID_SIGNATURE", "", 401)
        if not client.set(rk.own(rk.presig_nonce(key_id, nonce)), "TRADEPULSE", nx=True, ex=NONCE_TTL_S):
            raise Refused("REPLAYED_NONCE", "", 401)
        return {"key_id": key_id, "allowed_strategies": row.get("allowed_strategies") or []}

    # --- validation ----------------------------------------------------
    def _validate(self, client, payload, key: dict, source_ip: str) -> dict:
        base, nd = contract.split(payload)
        raw_cal = client.get(rk.PRESIG_CALENDAR)
        if not raw_cal:
            raise Refused("TEMPLATES_NOT_PUBLISHED", "the OMS has not published its calendar yet", 503)
        contract.check_session(nd, self.clock(), contract.Calendar.from_dict(json.loads(raw_cal)))
        sid, strategy_id = base.get("signal_id"), base.get("strategy_id")
        if not isinstance(sid, str) or not SIGNAL_ID_RE.match(sid):
            raise Refused("INVALID_SIGNAL_ID", "signal_id must be 6-64 chars of A-Z a-z 0-9 _ - : .")
        if not isinstance(strategy_id, str) or not STRATEGY_ID_RE.match(strategy_id):
            raise Refused("INVALID_STRATEGY_ID", "strategy_id must be 1-64 chars of A-Z a-z 0-9 _ -")
        allowed = key["allowed_strategies"]
        if "*" not in allowed and strategy_id not in allowed:
            raise Refused("STRATEGY_NOT_ALLOWED", "this algo key may not send this strategy", 403)
        raw_tpl = client.get(rk.presig_strategy(strategy_id))
        if not raw_tpl:
            raise Refused("STRATEGY_NOT_FOUND", f"unknown strategy_id {strategy_id}")
        tpl = json.loads(raw_tpl)
        if not tpl["strategy"].get("enabled"):
            raise Refused("STRATEGY_DISABLED", "strategy is disabled", 200)
        cfg = tpl.get("config")
        if not cfg:
            raise Refused("STRATEGY_CONFIG_MISSING", "strategy has no active configuration", 200)
        if not cfg.get("legs"):
            raise Refused("STRATEGY_HAS_NO_LEGS", "the active configuration defines no legs", 200)
        basket = str((cfg.get("params") or {}).get("strategy_mode") or "LEG").upper() == "BASKET"
        orders = base.get("orders")
        if not isinstance(orders, list) or not orders:
            raise Refused("NO_ORDERS", "orders must be a non-empty list")
        limit = MAX_BASKET_ORDERS if basket else MAX_LEG_ORDERS
        if len(orders) > limit:
            raise Refused("TOO_MANY_ORDERS", f"At most {limit} orders per signal")
        current = client.get(rk.PRESIG_INSTRUMENTS_CURRENT)
        if not current:
            raise Refused("TEMPLATES_NOT_PUBLISHED", "the OMS has not published its instrument master yet", 503)
        errors, tags, seen = [], set(), {}
        for i, o in enumerate(orders):
            err = self._order(client, rk.presig_instruments(current), i, o, tpl, basket, nd)
            if err:
                errors.append({"order_index": i, "leg_id": o.get("leg_id") if isinstance(o, dict) else None,
                               "tag": o.get("tag") if isinstance(o, dict) else None, "code": err[0], "message": err[1]})
                continue
            tags.add(o["tag"])
            k = (o.get("leg_id"), o["tag"]) if not basket else (o["tag"], o["exchange"].upper(), int(o["exchange_token"]))
            if k in seen:
                errors.append({"order_index": i, "leg_id": o.get("leg_id"), "tag": o["tag"],
                               "code": "DUPLICATE_LEG_TAG" if not basket else "DUPLICATE_INSTRUMENT",
                               "message": f"same as order {seen[k]}"})
            seen.setdefault(k, i)
        if errors:
            raise Refused("INVALID_ORDERS", "One or more orders failed validation", 422, errors)
        return {"signal_id": sid, "strategy_id": strategy_id, "trading_date": nd.trading_date, "version": nd.version,
                "policy": nd.policy, "tags": sorted(tags), "basket": basket, "config_version": cfg["version"],
                "payload": payload, "payload_sha256": payload_sha256(payload), "key_id": key["key_id"],
                "source": "TRADEPULSE", "source_ip": source_ip,
                "received_at": self.clock().astimezone(contract.IST).isoformat(timespec="milliseconds")}

    def _order(self, client, inst_key, i, o, tpl, basket, nd):
        """(code, message) for the first problem with one order, else None."""
        if not isinstance(o, dict):
            return "INVALID_ORDER", "Each order must be an object"
        tag = o.get("tag")
        if not isinstance(tag, str) or not TAG_RE.match(tag):
            return "INVALID_TAG", "tag is required on every order: 1-64 chars of A-Z a-z 0-9 _ - : ."
        side = str(o.get("side") or "").strip().upper()
        if side not in SIDES:
            return "INVALID_SIDE", "side must be BUY, SELL, SHORT or COVER"
        if SIDES[side] != "OPEN":
            return "NEXT_DAY_EXIT_NOT_SUPPORTED", (f"{side} closes a position; next-day signals only open positions - "
                                                   "send exits to the live webhook during the session")
        exchange = str(o.get("exchange") or "").strip().upper()
        if exchange not in EXCHANGES:
            return "INVALID_EXCHANGE", f"exchange must be one of {', '.join(EXCHANGES)}"
        token = o.get("exchange_token")
        if isinstance(token, bool) or not (isinstance(token, int) or (isinstance(token, str) and token.isdigit())) \
                or int(token) <= 0:
            return "INVALID_TOKEN", "exchange_token must be a positive integer"
        raw = client.hget(inst_key, f"{exchange}:{int(token)}")
        if not raw:
            return "INSTRUMENT_NOT_FOUND", f"{exchange}:{token} is not in the instrument master"
        cls, lot, tick, expiry, name, zsym, asym = json.loads(raw)
        symbol = zsym or asym or str(token)
        if expiry and expiry < nd.trading_date:
            return "INSTRUMENT_EXPIRED", f"{symbol} expired on {expiry}"
        cfg, strategy = tpl["config"], tpl["strategy"]
        params = cfg.get("params") or {}
        if basket:
            tid = CLASS_TEMPLATE.get(cls)
            leg = next((leg for leg in cfg["legs"] if leg["leg_id"] == tid), None)
            if leg is None:
                return "SEGMENT_NOT_ALLOWED", f"{symbol} is not a segment {strategy['strategy_id']} trades"
            category = leg.get("instrument_type") or ""
            defaults = ((params.get("basket") or {}).get("defaults") or {}).get(tid) or {}
            product = str(o.get("product") or defaults.get("product") or "").upper()
            order_type = str(o.get("order_type") or defaults.get("order_type") or "LIMIT").upper()
            underlying = leg.get("underlying") or ""
        else:
            leg_id = o.get("leg_id")
            if not isinstance(leg_id, str) or not LEG_ID_RE.match(leg_id):
                return "INVALID_LEG_ID", "leg_id is required: 1-32 chars of A-Z a-z 0-9 _ -"
            leg = next((leg for leg in cfg["legs"] if leg["leg_id"] == leg_id), None)
            if leg is None:
                return "INVALID_LEG_ID", f"{leg_id} is not a leg of {strategy['strategy_id']}"
            pct = _decimal(o.get("quantity_percent"))
            if pct is None or pct <= 0 or pct > 100:
                return "INVALID_QUANTITY_PERCENT", "quantity_percent must be greater than 0 and at most 100"
            category = strategy.get("category") or ""
            product = str(o.get("product") or "").upper()
            order_type = str(o.get("order_type") or "LIMIT").upper()
            underlying = leg.get("underlying") or strategy.get("underlying") or ""
            if category == "OPTIONS" and leg.get("option_type") and cls != leg["option_type"]:
                return "OPTION_TYPE_MISMATCH", f"{leg_id} is configured {leg['option_type']} but {symbol} is {cls}"
        if not leg.get("enabled", True):
            return "LEG_DISABLED", "this leg / template is disabled"
        if cls not in CATEGORY_CLASSES.get(category, set()):
            return "INSTRUMENT_CATEGORY_MISMATCH", f"{symbol} is {cls}; this belongs to a {category} strategy"
        if leg.get("exchange") and leg["exchange"] != exchange:
            return "EXCHANGE_MISMATCH", f"trades on {leg['exchange']}, not {exchange}"
        if underlying and underlying.strip().upper() not in {name.upper(), zsym.upper(), asym.upper().split("-")[0]}:
            return "UNDERLYING_MISMATCH", f"{symbol} is not a {underlying} contract"
        if product not in CATEGORY_PRODUCTS.get(category, set()):
            return "INVALID_PRODUCT", f"{category} orders must use {' or '.join(sorted(CATEGORY_PRODUCTS.get(category, [])))}"
        if order_type not in ORDER_TYPES or (order_type == "SL_LIMIT" and not basket):
            return "INVALID_ORDER_TYPE", "order_type must be LIMIT, LIMITMARKET or MARKET"
        price = _decimal(o.get("price")) if o.get("price") not in (None, "") else None
        if o.get("price") not in (None, "") and (price is None or price < 0):
            return "INVALID_PRICE", "price must be a number >= 0"
        price = price or None
        if order_type == "MARKET" and price is not None:
            return "INVALID_PRICE", "MARKET orders must not carry a price"
        if order_type == "MARKET" and not tpl.get("settings", {}).get("allow_market_orders"):
            return "MARKET_ORDERS_DISABLED", "MARKET orders are disabled; send a LIMIT order"
        if price is not None and tick and tpl.get("settings", {}).get("price_tick_policy") != "ROUND" \
                and price % Decimal(str(tick)) != 0:
            return "PRICE_NOT_ON_TICK", f"price {price} is not a multiple of tick size {tick}"
        if nd.policy == "PREMARKET" and (order_type != "LIMIT" or price is None):
            return "PREMARKET_NEEDS_LIMIT_PRICE", "a pre-market order must be order_type LIMIT with a price"
        return None
