"""Read-only broker snapshots: positions and holdings. Nothing else, ever.

Rules (the user's operating constraints):

* **Two read-only endpoints per broker** - positions and holdings. No order, trade-book, quote or
  margin endpoint exists in this module (a test enforces it).
* **One request per 3 seconds across every account and process** - a Redis slot
  (``SET tradepulse:broker:slot NX PX 3000``). Scheduled reconciles wait for the slot; a user request
  that finds it taken is refused with "please wait N s" instead of queueing.
* **Only when asked** - at the 09:00 / 09:05 reconcile or an explicit user refresh. There is no
  background polling. Between those, the book follows order events (see ``book``).
* **No retries, one pooled connection, 10 s timeout** - a failed read is recorded, not repeated.
* **Credentials come from Redis, read-only**: the session the OMS's generator wrote
  (``smx:session:<account>``) and the account's API key (``smx:oms:apikey:<account>``). Nothing is
  stored, logged or returned; the OMS is never contacted.

Paper accounts are read from the OMS's paper book in Redis (``smx:oms:paper:*``) - no broker, no slot.
"""

from __future__ import annotations

import json
import logging

import httpx

from .config import today_ist

log = logging.getLogger("tradepulse.brokers")

SLOT_KEY = "tradepulse:broker:slot"
PRODUCT_FROM_ANGEL = {"INTRADAY": "MIS", "CARRYFORWARD": "NRML", "DELIVERY": "CNC", "MARGIN": "MTF", "MTF": "MTF"}
FIVEPAISA_EXCHANGES = {("N", "C"): "NSE", ("B", "C"): "BSE", ("N", "D"): "NFO", ("B", "D"): "BFO"}

# The complete list of broker endpoints TradePulse may call. Read-only.
ENDPOINTS = {
    ("zerodha", "positions"): ("GET", "/portfolio/positions"),
    ("zerodha", "holdings"): ("GET", "/portfolio/holdings"),
    ("angel", "positions"): ("GET", "/rest/secure/angelbroking/order/v1/getPosition"),
    ("angel", "holdings"): ("GET", "/rest/secure/angelbroking/portfolio/v1/getHolding"),
    ("5paisa", "positions"): ("POST", "V2/NetPositionNetWise"),
    ("5paisa", "holdings"): ("POST", "V3/Holding"),
}


class BrokerReadError(Exception):
    pass


class Busy(Exception):
    """The global slot is taken: the caller must wait ``wait_ms``."""

    def __init__(self, wait_ms: int):
        super().__init__(f"broker slot busy for {wait_ms} ms")
        self.wait_ms = wait_ms


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _g(d: dict, *names, default=None):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


class BrokerReaders:
    def __init__(self, bus, settings, store=None, http: httpx.Client | None = None):
        self.bus, self.settings, self.store = bus, settings, store
        self._http = http
        self.kite_api = (getattr(settings, "KITE_API", "") or "https://api.kite.trade").rstrip("/")
        self.angel_api = (getattr(settings, "ANGEL_API", "") or "https://apiconnect.angelone.in").rstrip("/")
        self.fivepaisa_api = getattr(settings, "FIVEPAISA_API", "") or "https://Openapi.5paisa.com/VendorsAPI/Service1.svc/"
        self.stats = {"requests": 0, "errors": 0, "busy_refusals": 0, "last_request_at": "", "last_error": ""}

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0),
                                      limits=httpx.Limits(max_connections=2, max_keepalive_connections=1))
        return self._http

    # --- the global slot ----------------------------------------------------------------------------
    def slot_ms(self) -> int:
        secs = 3
        if self.store is not None:
            try:
                secs = max(3, int(self.store.setting("broker_slot_seconds") or 3))
            except (TypeError, ValueError):
                secs = 3
        return secs * 1000

    def try_slot(self) -> None:
        ok, wait = self.bus.acquire_slot(SLOT_KEY, self.slot_ms())
        if not ok:
            self.stats["busy_refusals"] += 1
            raise Busy(wait)

    def wait_slot(self, stop=None) -> None:
        import time
        while True:
            ok, wait = self.bus.acquire_slot(SLOT_KEY, self.slot_ms())
            if ok:
                return
            if stop is not None and stop.is_set():
                raise BrokerReadError("stopping")
            time.sleep(min(wait, 3000) / 1000 + 0.01)

    # --- session (read-only from Redis) ----------------------------------------------------------------
    def session(self, account_id: str) -> dict:
        data = self.bus.hgetall_any(f"smx:session:{account_id}") or {}
        if data.get("status") != "OK" or not data.get("access_token"):
            raise BrokerReadError(f"no valid broker session in Redis (status {data.get('status') or 'missing'})")
        updated = str(data.get("updated_date") or "")[:10]
        if updated and updated < today_ist():
            raise BrokerReadError(f"broker session is from {updated}; today's session has not been generated")
        api_key = self.bus.get_any(f"smx:oms:apikey:{account_id}")
        if not api_key:
            raise BrokerReadError("no API key published for this account (smx:oms:apikey)")
        return {"access_token": data["access_token"], "api_key": api_key}

    # --- public --------------------------------------------------------------------------------------
    def needs_slot(self, broker: str) -> bool:
        return str(broker).lower() != "paper"

    def fetch(self, account_id: str, broker: str, kind: str) -> list[dict]:
        """One snapshot. The caller must already hold the slot (``try_slot``/``wait_slot``) unless paper."""
        broker = str(broker).lower()
        if kind not in ("positions", "holdings"):
            raise ValueError(kind)
        if broker == "paper":
            return self._paper(account_id, kind)
        if (broker, kind) not in ENDPOINTS:
            raise BrokerReadError(f"{broker} is not supported for {kind}")
        s = self.session(account_id)
        from .config import ist_iso
        self.stats["requests"] += 1
        self.stats["last_request_at"] = ist_iso()
        try:
            if broker == "zerodha":
                return self._zerodha(s, kind)
            if broker == "angel":
                return self._angel(s, kind)
            return self._fivepaisa(s, account_id, kind)
        except BrokerReadError as ex:
            self.stats["errors"] += 1
            self.stats["last_error"] = str(ex)[:200]
            raise
        except httpx.HTTPError as ex:
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{broker} {kind}: {type(ex).__name__}"
            raise BrokerReadError(f"{broker} {kind}: network error ({type(ex).__name__})") from None
        finally:
            # The 3 s are counted from when the broker ANSWERED, not from when the slot was taken: a slow
            # request (cold TLS, broker latency) must never let the next one reach the broker sooner.
            self.bus.restart_slot(SLOT_KEY, self.slot_ms())

    # --- Zerodha (Kite Connect v3) -------------------------------------------------------------------
    def _zerodha(self, s: dict, kind: str) -> list[dict]:
        _m, path = ENDPOINTS[("zerodha", kind)]
        r = self.http.get(self.kite_api + path, headers={"X-Kite-Version": "3",
                                                         "Authorization": f"token {s['api_key']}:{s['access_token']}"})
        data = _json(r, "zerodha")
        if data.get("status") != "success":
            raise BrokerReadError(f"zerodha {kind}: {data.get('message') or r.status_code}")
        if kind == "positions":
            return [{"exchange": p.get("exchange", ""), "exchange_token": _int(p.get("instrument_token")) >> 8,
                     "product": str(p.get("product") or "").upper(), "net_quantity": _int(p.get("quantity")),
                     "broker_symbol": p.get("tradingsymbol") or "", "average_price": _float(p.get("average_price")),
                     "last_price": _float(p.get("last_price")), "pnl": _float(p.get("pnl"))}
                    for p in ((data.get("data") or {}).get("net") or [])]
        out = []
        for h in data.get("data") or []:
            settled, t1, pledged = _int(h.get("quantity")), _int(h.get("t1_quantity")), _int(h.get("collateral_quantity"))
            out.append({"exchange": h.get("exchange", ""), "exchange_token": _int(h.get("instrument_token")) >> 8,
                        "product": str(h.get("product") or "CNC").upper(), "broker_symbol": h.get("tradingsymbol") or "",
                        "isin": h.get("isin") or "", "quantity": settled + t1 + pledged, "sellable": max(0, settled),
                        "t1_quantity": t1, "pledged_quantity": pledged, "average_price": _float(h.get("average_price")),
                        "last_price": _float(h.get("last_price")), "pnl": _float(h.get("pnl"))})
        return out

    # --- Angel One (SmartAPI REST) -------------------------------------------------------------------
    def _angel(self, s: dict, kind: str) -> list[dict]:
        _m, path = ENDPOINTS[("angel", kind)]
        r = self.http.get(self.angel_api + path, headers={
            "Authorization": f"Bearer {s['access_token']}", "X-PrivateKey": s["api_key"], "X-UserType": "USER",
            "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00", "Accept": "application/json", "Content-Type": "application/json"})
        data = _json(r, "angel")
        if not data.get("status"):
            raise BrokerReadError(f"angel {kind}: {data.get('message') or data.get('errorcode') or r.status_code}")
        rows = data.get("data") or []
        if isinstance(rows, dict):                       # getAllHolding shape
            rows = rows.get("holdings") or []
        if kind == "positions":
            return [{"exchange": p.get("exchange", ""), "exchange_token": _int(p.get("symboltoken")),
                     "product": PRODUCT_FROM_ANGEL.get(str(p.get("producttype", "")).upper(), "NRML"),
                     "net_quantity": _int(p.get("netqty")), "broker_symbol": p.get("tradingsymbol") or "",
                     "average_price": _angel_average(p),
                     "last_price": _float(p.get("ltp")), "pnl": _float(p.get("pnl"))} for p in rows]
        out = []
        for h in rows:
            quantity, t1 = _int(h.get("quantity")), _int(h.get("t1quantity"))
            product = PRODUCT_FROM_ANGEL.get(str(h.get("product", "")).upper(), "CNC")
            pledged = 0 if product == "MTF" else _int(h.get("collateralquantity"))
            sellable = max(0, quantity - t1 - pledged)
            realised = _int(h.get("realisedquantity"))
            if 0 < realised < sellable:
                sellable = realised
            out.append({"exchange": h.get("exchange", ""), "exchange_token": _int(h.get("symboltoken")),
                        "product": product, "broker_symbol": h.get("tradingsymbol") or "", "isin": h.get("isin") or "",
                        "quantity": quantity, "sellable": sellable, "t1_quantity": t1, "pledged_quantity": pledged,
                        "average_price": _float(h.get("averageprice")), "last_price": _float(h.get("ltp") or h.get("close")),
                        "pnl": _float(h.get("profitandloss"))})
        return out

    # --- 5paisa (OpenAPI) ------------------------------------------------------------------------------
    def _fivepaisa(self, s: dict, account_id: str, kind: str) -> list[dict]:
        _m, route = ENDPOINTS[("5paisa", kind)]
        r = self.http.post(self.fivepaisa_api + route, json={"head": {"Key": s["api_key"]}, "body": {"ClientCode": account_id}},
                           headers={"Authorization": f"bearer {s['access_token']}"})
        data = _json(r, "5paisa")
        body = data.get("body") or {}
        if str(body.get("Status", "0")) != "0" and "no record" not in str(body.get("Message", "")).lower():
            raise BrokerReadError(f"5paisa {kind}: {body.get('Message') or 'status ' + str(body.get('Status'))}")
        if kind == "positions":
            out = []
            for p in body.get("NetPositionDetail") or []:
                exch = FIVEPAISA_EXCHANGES.get((str(p.get("Exch", ""))[:1], str(p.get("ExchType", ""))[:1]))
                if not exch:
                    continue
                net = _int(p.get("NetQty"))
                product = "MIS" if str(p.get("OrderFor", "")).upper().startswith("I") else ("NRML" if exch in ("NFO", "BFO") else "CNC")
                out.append({"exchange": exch, "exchange_token": _int(p.get("ScripCode")), "product": product,
                            "net_quantity": net, "broker_symbol": str(_g(p, "ScripName", "Symbol", default="")),
                            "average_price": _float(_g(p, "BuyAvgRate", default=0) if net >= 0 else _g(p, "SellAvgRate", default=0)),
                            "last_price": _float(_g(p, "LTP", default=0)),
                            "pnl": _float(_g(p, "MTOM", default=0)) + _float(_g(p, "BookedPL", default=0))})
            return out
        out = []
        for h in body.get("Data") or []:
            q = _int(_g(h, "Quantity", "Qty", default=0))
            free = max(0, min(_int(_g(h, "SellableQty", "DPQty", "FreeQty", default=q)), q))
            out.append({"exchange": "NSE" if str(_g(h, "Exch", default="N")).upper().startswith("N") else "BSE",
                        "exchange_token": _int(_g(h, "ScripCode", default=0)), "product": "CNC",
                        "broker_symbol": str(_g(h, "Symbol", "ScripName", default="")), "isin": str(_g(h, "ISIN", default="")),
                        "quantity": q, "sellable": free, "t1_quantity": 0, "pledged_quantity": max(0, q - free),
                        "average_price": _float(_g(h, "AvgRate", default=0)),
                        "last_price": _float(_g(h, "CurrentPrice", "LTP", default=0)), "pnl": 0.0})
        return out

    # --- paper: the OMS's paper book in Redis (no broker) ----------------------------------------------
    def _paper(self, account_id: str, kind: str) -> list[dict]:
        rows = []
        for key in (f"smx:oms:paper:pos:{account_id}", f"smx:oms:paper:posday:{account_id}"):
            for raw in (self.bus.hgetall_any(key) or {}).values():
                try:
                    rows.append(json.loads(raw))
                except (TypeError, ValueError):
                    continue
        positions = [{"exchange": p.get("exchange", ""), "exchange_token": _int(p.get("exchange_token")),
                      "product": str(p.get("product") or "").upper(), "net_quantity": _int(p.get("quantity")),
                      "broker_symbol": p.get("broker_symbol") or p.get("symbol") or "",
                      "instrument_class": p.get("instrument_class") or "",
                      "average_price": _float(p.get("average_price")), "last_price": 0.0,
                      "pnl": round(_float(p.get("sell_value")) - _float(p.get("buy_value")), 2) if not _int(p.get("quantity")) else 0.0}
                     for p in rows]
        if kind == "positions":
            return positions
        return [{"exchange": p["exchange"], "exchange_token": p["exchange_token"], "product": p["product"],
                 "broker_symbol": p["broker_symbol"], "isin": "", "quantity": p["net_quantity"], "sellable": p["net_quantity"],
                 "t1_quantity": 0, "pledged_quantity": 0, "average_price": p["average_price"], "last_price": 0.0, "pnl": 0.0}
                for p in positions if p["product"] in ("CNC", "MTF") and p["net_quantity"] > 0]


def _angel_average(p: dict) -> float:
    """Angel reports avgnetprice/netprice as 0 for a position carried overnight with no trade today; the
    carried price is then in cfbuyavgprice / cfsellavgprice. Fall back by the side of the net position."""
    for k in ("avgnetprice", "netprice"):
        v = _float(p.get(k))
        if v > 0:
            return v
    long_ = _int(p.get("netqty")) > 0
    keys = ("cfbuyavgprice", "buyavgprice", "totalbuyavgprice") if long_ else ("cfsellavgprice", "sellavgprice", "totalsellavgprice")
    for k in keys:
        v = _float(p.get(k))
        if v > 0:
            return v
    return 0.0                                   # unknown: the book then uses the broker's own P&L, never avg 0


def _json(r: httpx.Response, broker: str) -> dict:
    if r.status_code in (401, 403):
        raise BrokerReadError(f"{broker}: session rejected ({r.status_code})")
    try:
        data = r.json()
    except ValueError:
        raise BrokerReadError(f"{broker}: non-JSON response ({r.status_code})") from None
    if not isinstance(data, dict):
        raise BrokerReadError(f"{broker}: unexpected response")
    return data
