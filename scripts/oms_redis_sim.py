"""Simulates what the SkyMatrix OMS writes to Redis - for tests and demos. Never point it at production Redis.

Writes exactly the OMS's key/stream shapes:
  smx:roster (with credentials, as the dashboard publishes it), smx:oms:apikeys, smx:oms:apikey:<acct>,
  smx:session:<acct>, smx:oms:cmd:w-<acct> (EXECUTE), smx:oms:events (worker reports), smx:oms:paper:pos:<acct>.

    python scripts/oms_redis_sim.py --redis redis://127.0.0.1:6390/0 seed
    python scripts/oms_redis_sim.py --redis redis://127.0.0.1:6390/0 run --n 20 --delay 2
"""

from __future__ import annotations

import argparse
import json
import random
import string
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
EVENTS = "smx:oms:events"

CLIENTS = [
    # username, name, account, broker, mode
    ("mathew", "Mathew", "ZM4521", "zerodha", "LIVE"),
    ("mathew", "Mathew", "AM88213", "angel", "LIVE"),
    ("jayesh", "Jayesh Mehta", "J252505", "angel", "LIVE"),
    ("bharti", "Bharti Raval", "BR77104", "zerodha", "LIVE"),
    ("bharti", "Bharti Raval", "BRP0091", "paper", "PAPER"),
    ("kiran", "Kiran Shah", "KS50310", "5paisa", "LIVE"),
]
INSTRUMENTS = [
    # exchange, token, symbol, class, lot, ref price, product
    ("NFO", 56723, "NIFTY26NOV26000CE", "CE", 75, 125.5, "NRML"),
    ("NFO", 56724, "NIFTY26NOV25800PE", "PE", 75, 98.0, "NRML"),
    ("NFO", 66110, "BANKNIFTY26NOV55000CE", "CE", 35, 410.0, "MIS"),
    ("NSE", 2885, "RELIANCE-EQ", "EQ", 1, 1312.4, "CNC"),
    ("NSE", 1594, "INFY-EQ", "EQ", 1, 1520.0, "CNC"),
]


class Writer:
    """Uniform writes for a redis.Redis client or the in-process MemoryBus (its *_foreign helpers)."""

    def __init__(self, target):
        self.t = target

    def xadd(self, stream, msg_type, data):
        fields = {"type": msg_type, "data": json.dumps(data, default=str, separators=(",", ":"))}
        if hasattr(self.t, "add_foreign"):
            return self.t.add_foreign(stream, fields)
        return self.t.xadd(stream, fields)

    def set(self, key, value):
        if hasattr(self.t, "set_foreign"):
            return self.t.set_foreign(key, value)
        return self.t.set(key, value)

    def hset(self, key, mapping):
        if hasattr(self.t, "hset_foreign"):
            return self.t.hset_foreign(key, mapping)
        return self.t.hset(key, mapping={k: str(v) for k, v in mapping.items()})


def rid(prefix):
    return prefix + "".join(random.choices(string.ascii_uppercase + string.digits, k=14))


class OmsSim:
    def __init__(self, target, clients=CLIENTS):
        self.w = Writer(target)
        self.clients = clients
        self.accounts = {c[2]: {"username": c[0], "name": c[1], "broker": c[3], "mode": c[4]} for c in clients}
        self.paper: dict[str, dict] = {}

    def seed(self, session_date: str | None = None):
        day = session_date or datetime.now(IST).isoformat(timespec="seconds")
        roster = [{"name": c[1], "account_id": c[2], "username": c[0], "email": f"{c[0]}@example.com", "broker": c[3],
                   "api_key": f"KEY-{c[2]}", "secret_key": f"SECRET-{c[2]}", "totp_key": "JBSWY3DPEHPK3PXP", "mpin": "1234",
                   "login_url": "", "password": "pw", "vendor_user_id": ""} for c in self.clients]
        self.w.set("smx:roster", json.dumps(roster))
        self.w.set("smx:oms:apikeys", json.dumps([c[2] for c in self.clients]))
        for c in self.clients:
            self.w.set(f"smx:oms:apikey:{c[2]}", f"KEY-{c[2]}")
            self.w.hset(f"smx:session:{c[2]}", {"account_id": c[2], "access_token": f"TOKEN-{c[2]}", "jwt_token": "",
                                                 "feed_token": "", "refresh_token": "", "updated_date": day, "status": "OK",
                                                 "last_error": ""})
            self.heartbeat(c[2])
        return self

    def heartbeat(self, account):
        a = self.accounts[account]
        self.w.xadd(EVENTS, "HEARTBEAT", {"worker_id": f"w-{account.lower()}", "account_id": account, "status": "READY",
                                          "broker": a["broker"], "broker_connected": True, "open_orders": 0, "at_ns": time.time_ns()})

    def _evt(self, account, msg_type, data):
        self.w.xadd(EVENTS, msg_type, {"worker_id": f"w-{account.lower()}", "account_id": account, "at_ns": time.time_ns(), **data})

    def order(self, account, outcome="fill", side="BUY", instrument=None, lots=1, delay=0.0, reject_message="RMS: margin exceeds",
              partial_fraction=0.5, price=None):
        """EXECUTE + the worker's reports, the way the OMS publishes them. Returns order info."""
        a = self.accounts[account]
        exch, token, symbol, cls, lot, ref, product = instrument or random.choice(INSTRUMENTS)
        qty = lot * lots
        price = round(price if price is not None else ref * random.uniform(0.99, 1.01), 2)
        oid, exe, boid = rid("O"), rid("X"), str(random.randint(10**14, 10**15 - 1))
        order = {"internal_order_id": oid, "broker_tag": rid("B"), "leg_id": "LEG_1", "leg_index": 0, "slice_no": 1, "slice_count": 1,
                 "priority": 0, "exchange": exch, "exchange_token": token, "symbol": symbol, "broker_symbol": symbol,
                 "instrument_class": cls, "lot_size": lot, "tick_size": 0.05, "side": side, "position_effect": "OPEN",
                 "product": product, "order_type": "LIMIT", "ordered_quantity": qty, "price": price, "price_source": "LTP",
                 "reference_price": None, "trigger_price": None, "oco_group": ""}
        self.w.xadd(f"smx:oms:cmd:w-{account.lower()}", "EXECUTE",
                    {"execution_id": exe, "signal_id": rid("S"), "strategy_id": "StratDemo", "account_id": account,
                     "broker": a["broker"], "worker_id": f"w-{account.lower()}", "trading_mode": a["mode"], "execution_mode": "PARALLEL",
                     "routed_ns": time.time_ns(), "orders": [order]})
        time.sleep(delay)
        self._evt(account, "ORDER_SUBMITTING", {"internal_order_id": oid, "price": price, "price_source": "LTP"})
        if outcome == "reject":
            self._evt(account, "ORDER_STATUS", {"internal_order_id": oid, "status": "REJECTED", "filled_quantity": 0,
                                                "message": reject_message, "source": "BROKER"})
            return {"internal_order_id": oid, "broker_order_id": "", "status": "REJECTED", "symbol": symbol, "qty": qty}
        time.sleep(delay)
        self._evt(account, "ORDER_ACCEPTED", {"internal_order_id": oid, "broker_order_id": boid})
        if outcome == "open":
            return {"internal_order_id": oid, "broker_order_id": boid, "status": "OPEN", "symbol": symbol, "qty": qty}
        if outcome == "cancel":
            time.sleep(delay)
            self._evt(account, "ORDER_STATUS", {"internal_order_id": oid, "status": "CANCELLED", "filled_quantity": 0,
                                                "broker_order_id": boid, "message": "cancelled by user"})
            return {"internal_order_id": oid, "broker_order_id": boid, "status": "CANCELLED", "symbol": symbol, "qty": qty}
        filled = max(1, int(qty * partial_fraction)) if outcome == "partial" else qty
        time.sleep(delay)
        fp = round(price * random.uniform(0.998, 1.0), 2)
        fid = rid("F")
        self._evt(account, "FILL", {"internal_order_id": oid, "fill_id": fid, "quantity": filled, "price": fp,
                                    "filled_at": datetime.now(IST).isoformat(timespec="milliseconds"), "broker_order_id": boid,
                                    "exchange_trade_id": str(random.randint(10**8, 10**9)), "source": "BROKER_TRADEBOOK"})
        status = "PARTIALLY_FILLED" if outcome == "partial" else "COMPLETE"
        self._evt(account, "ORDER_STATUS", {"internal_order_id": oid, "status": status, "filled_quantity": filled,
                                            "average_price": fp, "broker_order_id": boid, "source": "BROKER_POLL"})
        if a["broker"] == "paper":
            self._paper_apply(account, exch, token, symbol, cls, product, side, filled, fp)
        return {"internal_order_id": oid, "broker_order_id": boid, "status": status, "symbol": symbol, "qty": qty,
                "filled": filled, "price": fp, "fill_id": fid, "exchange": exch, "token": token, "product": product}

    def _paper_apply(self, account, exch, token, symbol, cls, product, side, qty, price):
        key = f"{exch}:{token}:{product}"
        p = self.paper.setdefault(account, {}).setdefault(key, {"contract": key, "exchange": exch, "exchange_token": token,
                                                               "product": product, "broker_symbol": symbol, "instrument_class": cls,
                                                               "quantity": 0, "average_price": 0.0, "buy_value": 0.0, "sell_value": 0.0})
        signed = qty if side == "BUY" else -qty
        if p["quantity"] == 0 or (p["quantity"] > 0) == (signed > 0):
            p["average_price"] = (abs(p["quantity"]) * p["average_price"] + qty * price) / abs(p["quantity"] + signed)
        p["quantity"] += signed
        p["buy_value" if side == "BUY" else "sell_value"] += qty * price
        self.w.hset(f"smx:oms:paper:pos:{account}", {key: json.dumps(p)})

    def position_book(self, account, positions: list[dict]):
        """The OMS worker publishing a POSITION_BOOK refresh (e.g. its 15:40 daily book)."""
        self._evt(account, "POSITION_BOOK", {"trading_mode": self.accounts[account]["mode"], "positions": positions, "error": ""})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    r = sub.add_parser("run")
    r.add_argument("--n", type=int, default=10)
    r.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args(argv)
    import redis
    sim = OmsSim(redis.Redis.from_url(args.redis, decode_responses=True))
    if args.cmd == "seed":
        sim.seed()
        print("seeded roster, sessions, api keys, heartbeats")
        return
    for _ in range(args.n):
        acct = random.choice([c[2] for c in CLIENTS])
        res = sim.order(acct, outcome=random.choice(["fill", "fill", "partial", "reject", "cancel"]),
                        side=random.choice(["BUY", "SELL"]), delay=args.delay / 4)
        print(acct, res["status"], res["symbol"], flush=True)
        time.sleep(args.delay)


if __name__ == "__main__":
    main()
