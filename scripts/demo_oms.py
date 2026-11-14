"""Demo OMS database for development and tests - NEVER points at the real OMS.

Builds a SQLite file with the subset of the SkyMatrix OMS schema TradePulse reads (same table and
column names as backend/db.py + backend/oms/migrations.py), seeds the three initial clients from
req.md plus a manager and an admin, and can simulate order lifecycles exactly the way the OMS
records them (oms_orders + oms_order_events + oms_fills + oms_trades/oms_trade_legs + oms_alerts).

    python scripts/demo_oms.py create data/demo_oms.db          # fresh demo DB
    python scripts/demo_oms.py simulate data/demo_oms.db --n 5  # append live-looking activity
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import string
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt

IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL,
    email TEXT, phone TEXT, role TEXT NOT NULL, auth_hash TEXT NOT NULL, manager_id INTEGER, status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_by INTEGER, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '', last_login_at TEXT);
CREATE TABLE IF NOT EXISTS broker_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, account_id TEXT NOT NULL UNIQUE, broker TEXT NOT NULL,
    api_key_enc TEXT, secret_key_enc TEXT, totp_key_enc TEXT, mpin_enc TEXT, login_url TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '', login_password_enc TEXT, vendor_user_id TEXT,
    auth_method TEXT NOT NULL DEFAULT 'AUTO');
CREATE TABLE IF NOT EXISTS broker_sessions (
    account_id TEXT PRIMARY KEY, access_token TEXT, refresh_token TEXT, feed_token TEXT, jwt_token TEXT, updated_date TEXT,
    status TEXT, last_error TEXT, synced_at TEXT);
CREATE TABLE IF NOT EXISTS web_sessions (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, ip TEXT, user_agent TEXT);
CREATE TABLE IF NOT EXISTS oms_account_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL UNIQUE, broker TEXT, automation_enabled INTEGER DEFAULT 1,
    trading_mode TEXT DEFAULT 'LIVE', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS oms_workers (
    worker_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, desired_state TEXT, status TEXT, private_ip TEXT, broker_connected INTEGER,
    redis_connected INTEGER, last_heartbeat_at TEXT, started_at TEXT, version TEXT, last_error TEXT, created_at TEXT, updated_at TEXT,
    broker TEXT, egress_ip TEXT, open_orders INTEGER DEFAULT 0, details TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS oms_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, internal_order_id TEXT NOT NULL UNIQUE, execution_id TEXT DEFAULT '', signal_id TEXT DEFAULT '',
    strategy_id TEXT DEFAULT '', client_user_id INTEGER, account_id TEXT NOT NULL, broker TEXT NOT NULL, worker_id TEXT DEFAULT '',
    trading_mode TEXT NOT NULL DEFAULT 'LIVE', tag TEXT DEFAULT '', exchange TEXT NOT NULL, exchange_token INTEGER DEFAULT 0,
    symbol TEXT NOT NULL, broker_symbol TEXT DEFAULT '', instrument_class TEXT DEFAULT '', lot_size INTEGER DEFAULT 1,
    signal_side TEXT DEFAULT '', side TEXT NOT NULL, position_effect TEXT DEFAULT 'OPEN', product TEXT DEFAULT 'NRML',
    order_type TEXT DEFAULT 'LIMIT', ordered_quantity INTEGER NOT NULL, price REAL, status TEXT NOT NULL, status_message TEXT DEFAULT '',
    broker_order_id TEXT DEFAULT '', filled_quantity INTEGER DEFAULT 0, average_price REAL DEFAULT 0, modification_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, trade_id TEXT DEFAULT '', trade_leg_id TEXT DEFAULT '', direction TEXT DEFAULT 'LONG');
CREATE TABLE IF NOT EXISTS oms_order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, internal_order_id TEXT NOT NULL, execution_id TEXT DEFAULT '', from_status TEXT,
    to_status TEXT NOT NULL, source TEXT NOT NULL, broker_order_id TEXT DEFAULT '', filled_quantity INTEGER DEFAULT 0,
    average_price REAL DEFAULT 0, message TEXT DEFAULT '', payload TEXT DEFAULT '{}', at TEXT NOT NULL, at_ns INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS oms_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fill_id TEXT NOT NULL UNIQUE, internal_order_id TEXT NOT NULL, execution_id TEXT DEFAULT '',
    account_id TEXT NOT NULL, broker TEXT NOT NULL, broker_order_id TEXT DEFAULT '', exchange_trade_id TEXT DEFAULT '',
    exchange TEXT DEFAULT '', exchange_token INTEGER DEFAULT 0, side TEXT, quantity INTEGER NOT NULL, price REAL NOT NULL,
    filled_at TEXT NOT NULL, source TEXT DEFAULT 'BROKER_TRADEBOOK', raw TEXT DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oms_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT NOT NULL UNIQUE, trading_mode TEXT NOT NULL, account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL, basket_id TEXT DEFAULT '', tag TEXT DEFAULT '', status TEXT NOT NULL, close_reason TEXT DEFAULT '',
    created_at TEXT NOT NULL, opened_at TEXT, closed_at TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oms_trade_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_leg_id TEXT NOT NULL UNIQUE, trade_id TEXT NOT NULL, leg_id TEXT DEFAULT 'LEG_1',
    direction TEXT NOT NULL, exchange TEXT NOT NULL, exchange_token INTEGER DEFAULT 0, product TEXT DEFAULT 'NRML',
    symbol TEXT NOT NULL, broker_symbol TEXT DEFAULT '', instrument_class TEXT DEFAULT '', lot_size INTEGER DEFAULT 1,
    ordered_quantity INTEGER DEFAULT 0, filled_quantity INTEGER DEFAULT 0, closed_quantity INTEGER DEFAULT 0,
    open_quantity INTEGER DEFAULT 0, average_entry_price REAL DEFAULT 0, average_exit_price REAL DEFAULT 0,
    status TEXT NOT NULL, close_reason TEXT DEFAULT '', opened_at TEXT, closed_at TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oms_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, severity TEXT NOT NULL, trading_mode TEXT DEFAULT '', account_id TEXT DEFAULT '',
    broker TEXT DEFAULT '', strategy_id TEXT DEFAULT '', tag TEXT DEFAULT '', trade_id TEXT DEFAULT '', internal_order_id TEXT DEFAULT '',
    symbol TEXT DEFAULT '', side TEXT DEFAULT '', order_status TEXT DEFAULT '', ordered_quantity INTEGER DEFAULT 0,
    unfilled_quantity INTEGER DEFAULT 0, message TEXT NOT NULL, created_at TEXT NOT NULL, at_ns INTEGER DEFAULT 0,
    acknowledged_at TEXT, acknowledged_by TEXT);
CREATE TABLE IF NOT EXISTS oms_broker_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, trading_mode TEXT NOT NULL DEFAULT 'LIVE', exchange TEXT NOT NULL DEFAULT '',
    exchange_token INTEGER NOT NULL DEFAULT 0, product TEXT NOT NULL DEFAULT '', broker_symbol TEXT NOT NULL DEFAULT '',
    net_quantity INTEGER NOT NULL DEFAULT 0, average_price REAL NOT NULL DEFAULT 0, last_price REAL NOT NULL DEFAULT 0,
    pnl REAL NOT NULL DEFAULT 0, raw TEXT NOT NULL DEFAULT '{}', reported_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
    UNIQUE (account_id, exchange, exchange_token, product));
"""

PEOPLE = [
    # username, name, role, password, manager username
    ("admin", "SkyMatrix Admin", "ADMIN", "admin123", None),
    ("rmanager", "Ravi Manager", "MANAGER", "manager123", None),
    ("mathew", "Mathew", "CLIENT", "mathew123", "rmanager"),
    ("jayesh", "Jayesh Mehta", "CLIENT", "jayesh123", "rmanager"),
    ("bharti", "Bharti Raval", "CLIENT", "bharti123", None),
    ("kiran", "Kiran Shah", "CLIENT", "kiran123", None),
]
ACCOUNTS = [
    # owner username, account id, broker, trading mode
    ("mathew", "ZM4521", "zerodha", "LIVE"),
    ("mathew", "AM88213", "angel", "LIVE"),
    ("jayesh", "J252505", "angel", "LIVE"),
    ("bharti", "BR77104", "zerodha", "LIVE"),
    ("bharti", "BRP0091", "paper", "PAPER"),
    ("kiran", "KS50310", "5paisa", "LIVE"),
]
INSTRUMENTS = [
    ("NFO", 56723, "NIFTY26NOV26000CE", "CE", 75, 125.5), ("NFO", 56724, "NIFTY26NOV25800PE", "PE", 75, 98.0),
    ("NFO", 66110, "BANKNIFTY26NOV55000CE", "CE", 35, 410.0), ("NSE", 2885, "RELIANCE-EQ", "EQ", 1, 1312.4),
    ("NSE", 1594, "INFY-EQ", "EQ", 1, 1520.0), ("NFO", 71001, "NIFTY26NOVFUT", "FUT", 75, 26080.0),
]


def now(dt: datetime | None = None) -> str:
    return (dt or datetime.now(IST)).isoformat(timespec="milliseconds")


def rid(prefix: str) -> str:
    return prefix + "".join(random.choices(string.ascii_uppercase + string.digits, k=14))


def connect(path) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def create(path: str | Path, seed_activity: bool = True, hash_rounds: int = 4) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    c = connect(path)
    c.executescript(SCHEMA)
    ids = {}
    for username, name, role, pw, _m in PEOPLE:
        h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=hash_rounds)).decode()
        ids[username] = c.execute("INSERT INTO users (username, name, role, auth_hash, status, created_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?)",
                                  (username, name, role, h, now())).lastrowid
    for username, *_r, mgr in PEOPLE:
        if mgr:
            c.execute("UPDATE users SET manager_id = ? WHERE id = ?", (ids[mgr], ids[username]))
    for owner, acct, broker, mode in ACCOUNTS:
        c.execute("INSERT INTO broker_accounts (user_id, account_id, broker, secret_key_enc, status, created_at) VALUES (?, ?, ?, '', 'ACTIVE', ?)",
                  (ids[owner], acct, broker, now()))
        c.execute("INSERT INTO broker_sessions (account_id, access_token, status, updated_date) VALUES (?, 'redacted-token', 'OK', ?)",
                  (acct, now()))
        c.execute("INSERT INTO oms_account_settings (account_id, broker, trading_mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                  (acct, broker, mode, now(), now()))
        c.execute("""INSERT INTO oms_workers (worker_id, account_id, desired_state, status, broker, broker_connected, redis_connected,
                     last_heartbeat_at, open_orders) VALUES (?, ?, 'RUNNING', 'READY', ?, 1, 1, ?, 0)""",
                  (f"w-{acct.lower()}", acct, broker, now()))
    c.commit()
    if seed_activity:
        random.seed(7)
        for acct, *_ in [(a[1],) for a in ACCOUNTS[:5]]:
            for _ in range(2):
                simulate_order(c, acct, outcome=random.choice(["fill", "fill", "partial", "reject", "cancel"]))
        simulate_order(c, "J252505", outcome="reject",
                       reject_message="Your order has been rejected due to Insufficient Funds. Available funds - Rs. 0.00.")
        raise_alert(c, "J252505", "SHORT_FAILED", "CRITICAL", "Exit order rejected: insufficient funds", "NIFTY26NOV26000CE")
    c.close()
    return path


def _account(c, account_id: str) -> dict:
    r = c.execute("""SELECT b.account_id, b.broker, b.user_id, s.trading_mode FROM broker_accounts b
                     LEFT JOIN oms_account_settings s ON s.account_id = b.account_id WHERE b.account_id = ?""", (account_id,)).fetchone()
    if not r:
        raise SystemExit(f"unknown account {account_id}")
    return dict(r)


def _event(c, oid, frm, to, source, broker_order_id="", filled=0, avg=0.0, message="", at=None):
    c.execute("""INSERT INTO oms_order_events (internal_order_id, from_status, to_status, source, broker_order_id, filled_quantity,
                 average_price, message, at, at_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (oid, frm, to, source, broker_order_id, filled, avg, message, at or now(), time.time_ns()))
    c.execute("UPDATE oms_orders SET status = ?, filled_quantity = ?, average_price = ?, broker_order_id = COALESCE(NULLIF(?, ''), broker_order_id), "
              "status_message = ?, updated_at = ? WHERE internal_order_id = ?", (to, filled, avg, broker_order_id, message, at or now(), oid))


def simulate_order(c, account_id: str, outcome: str = "fill", side: str = "BUY", instrument=None, lots: int = 1,
                   reject_message: str = "RMS: margin exceeds available funds", step_delay: float = 0.0,
                   close_trade_id: str | None = None) -> dict:
    """Write one order lifecycle exactly as the OMS records it. outcome: fill | partial | reject | cancel | open."""
    a = _account(c, account_id)
    exch, token, symbol, cls, lot, ref = instrument or random.choice(INSTRUMENTS)
    qty = lot * lots
    price = round(ref * random.uniform(0.98, 1.02), 2)
    oid, bid = rid("O"), str(random.randint(10**14, 10**15 - 1))
    effect = "CLOSE" if close_trade_id else "OPEN"
    direction = "LONG" if (side == "BUY") == (effect == "OPEN") else "SHORT"
    trade_id = close_trade_id or rid("T")
    leg_id = None
    t0 = now()
    if not close_trade_id:
        c.execute("INSERT INTO oms_trades (trade_id, trading_mode, account_id, strategy_id, status, created_at, opened_at, updated_at) "
                  "VALUES (?, ?, ?, 'StratDemo', 'OPEN', ?, ?, ?)", (trade_id, a["trading_mode"] or "LIVE", account_id, t0, t0, t0))
        leg_id = rid("L")
        c.execute("""INSERT INTO oms_trade_legs (trade_leg_id, trade_id, direction, exchange, exchange_token, symbol, broker_symbol,
                     instrument_class, lot_size, ordered_quantity, status, opened_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)""",
                  (leg_id, trade_id, direction, exch, token, symbol, symbol, cls, lot, qty, t0, t0))
    else:
        leg = c.execute("SELECT * FROM oms_trade_legs WHERE trade_id = ?", (trade_id,)).fetchone()
        leg_id = leg["trade_leg_id"]
        exch, token, symbol, cls, qty = leg["exchange"], leg["exchange_token"], leg["symbol"], leg["instrument_class"], leg["open_quantity"]
        direction = leg["direction"]
    c.execute("""INSERT INTO oms_orders (internal_order_id, account_id, client_user_id, broker, worker_id, trading_mode, exchange, exchange_token,
                 symbol, broker_symbol, instrument_class, lot_size, side, signal_side, position_effect, product, order_type, ordered_quantity, price,
                 status, created_at, updated_at, trade_id, trade_leg_id, direction, strategy_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NRML', 'LIMIT', ?, ?, 'ROUTED', ?, ?, ?, ?, ?, 'StratDemo')""",
              (oid, account_id, a["user_id"], a["broker"], f"w-{account_id.lower()}", a["trading_mode"] or "LIVE", exch, token, symbol,
               symbol, cls, lot, side, side, effect, qty, price, t0, t0, trade_id, leg_id, direction))
    _event(c, oid, "", "ROUTED", "OMS", at=t0)
    c.commit()
    time.sleep(step_delay)
    _event(c, oid, "ROUTED", "SUBMITTING", "WORKER", message=f"sending to broker at {price}")
    c.commit()
    time.sleep(step_delay)
    if outcome == "reject":
        _event(c, oid, "SUBMITTING", "REJECTED", "BROKER", message=reject_message)
        c.execute("UPDATE oms_trade_legs SET status = 'CANCELLED', close_reason = 'ENTRY_NOT_FILLED', updated_at = ? WHERE trade_leg_id = ?",
                  (now(), leg_id))
        c.commit()
        return {"internal_order_id": oid, "broker_order_id": "", "trade_id": trade_id, "status": "REJECTED"}
    _event(c, oid, "SUBMITTING", "OPEN", "BROKER", broker_order_id=bid, message="accepted by the broker")
    c.commit()
    time.sleep(step_delay)
    if outcome == "open":
        return {"internal_order_id": oid, "broker_order_id": bid, "trade_id": trade_id, "status": "OPEN"}
    if outcome == "cancel":
        _event(c, oid, "OPEN", "CANCELLED", "BROKER_POLL", broker_order_id=bid, message="cancelled by user")
        c.commit()
        return {"internal_order_id": oid, "broker_order_id": bid, "trade_id": trade_id, "status": "CANCELLED"}
    first = qty // 2 if outcome == "partial" and qty > 1 else qty
    fills = [first] + ([qty - first] if outcome == "fill" and first != qty else [])
    filled, value = 0, 0.0
    for fq in ([first] if outcome == "partial" else [qty]):
        fp = round(price * random.uniform(0.997, 1.0), 2)
        filled += fq
        value += fq * fp
        c.execute("""INSERT INTO oms_fills (fill_id, internal_order_id, account_id, broker, broker_order_id, exchange_trade_id, exchange,
                     exchange_token, side, quantity, price, filled_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (rid("F"), oid, account_id, a["broker"], bid, str(random.randint(10**8, 10**9)), exch, token, side, fq, fp, now(), now()))
    avg = round(value / filled, 2)
    final = "PARTIALLY_FILLED" if outcome == "partial" else "COMPLETE"
    _event(c, oid, "OPEN", final, "BROKER_POLL", broker_order_id=bid, filled=filled, avg=avg)
    t = now()
    if close_trade_id:
        c.execute("""UPDATE oms_trade_legs SET closed_quantity = closed_quantity + ?, open_quantity = open_quantity - ?, average_exit_price = ?,
                     status = CASE WHEN open_quantity - ? <= 0 THEN 'CLOSED' ELSE status END,
                     closed_at = CASE WHEN open_quantity - ? <= 0 THEN ? ELSE closed_at END, updated_at = ? WHERE trade_leg_id = ?""",
                  (filled, filled, avg, filled, filled, t, t, leg_id))
        c.execute("UPDATE oms_trades SET status = 'CLOSED', closed_at = ?, updated_at = ? WHERE trade_id = ?", (t, t, trade_id))
    else:
        c.execute("""UPDATE oms_trade_legs SET filled_quantity = ?, open_quantity = ?, average_entry_price = ?, status = 'OPEN', updated_at = ?
                     WHERE trade_leg_id = ?""", (filled, filled, avg, t, leg_id))
        ltp = round(avg * random.uniform(0.97, 1.05), 2)
        c.execute("""INSERT INTO oms_broker_positions (account_id, trading_mode, exchange, exchange_token, product, broker_symbol, net_quantity,
                     average_price, last_price, pnl, reported_at, updated_at) VALUES (?, ?, ?, ?, 'NRML', ?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(account_id, exchange, exchange_token, product) DO UPDATE SET net_quantity = net_quantity + excluded.net_quantity,
                     last_price = excluded.last_price, updated_at = excluded.updated_at""",
                  (account_id, a["trading_mode"] or "LIVE", exch, token, symbol, filled if direction == "LONG" else -filled, avg, ltp,
                   round((ltp - avg) * filled, 2), t, t))
    c.commit()
    return {"internal_order_id": oid, "broker_order_id": bid, "trade_id": trade_id, "status": final, "filled": filled, "avg": avg}


def raise_alert(c, account_id, kind, severity, message, symbol=""):
    a = _account(c, account_id)
    c.execute("""INSERT INTO oms_alerts (kind, severity, trading_mode, account_id, broker, symbol, message, created_at, at_ns)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (kind, severity, a["trading_mode"] or "LIVE", account_id, a["broker"], symbol,
                                                        message, now(), time.time_ns()))
    c.commit()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    cr = sub.add_parser("create")
    cr.add_argument("path")
    sim = sub.add_parser("simulate")
    sim.add_argument("path")
    sim.add_argument("--n", type=int, default=5)
    sim.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args(argv)
    if args.cmd == "create":
        p = create(args.path, hash_rounds=10)
        print(f"demo OMS created at {p}\nlogins: " + ", ".join(f"{u}/{pw} ({r})" for u, _n, r, pw, _m in PEOPLE))
    else:
        c = connect(args.path)
        for i in range(args.n):
            acct = random.choice([a[1] for a in ACCOUNTS])
            open_trade = c.execute("""SELECT t.trade_id FROM oms_trades t JOIN oms_trade_legs l ON l.trade_id = t.trade_id
                                      WHERE t.account_id = ? AND l.open_quantity > 0 ORDER BY RANDOM() LIMIT 1""", (acct,)).fetchone()
            if open_trade and random.random() < 0.4:
                r = simulate_order(c, acct, outcome="fill", side="SELL", close_trade_id=open_trade["trade_id"], step_delay=args.delay / 3)
            else:
                r = simulate_order(c, acct, outcome=random.choice(["fill", "fill", "partial", "reject", "cancel"]),
                                   side=random.choice(["BUY", "SELL"]), step_delay=args.delay / 3)
            print(f"{acct}: {r['status']} {r.get('broker_order_id') or r['internal_order_id']}")
            sys.stdout.flush()
            time.sleep(args.delay)


if __name__ == "__main__":
    main()
