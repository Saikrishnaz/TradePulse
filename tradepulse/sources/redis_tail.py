"""The OMS, seen only through Redis. No OMS database, no OMS API - so it works whatever the OMS host does.

What is read (never written - TradePulse writes only ``tradepulse:*``):

* ``smx:roster`` / ``smx:oms:apikeys``  - the accounts (client name, broker). The roster also carries
  credentials for the session generator; they are discarded the moment the JSON is parsed.
* ``smx:session:<account>``             - only ``status`` / ``updated_date`` here (session health).
* ``smx:oms:cmd:<worker>`` (EXECUTE/RESUME) - the full order: symbol, side, exchange, token, product,
  quantity, price, trading mode. Plain ``XREAD`` with saved cursors (no consumer group on OMS keys).
* ``smx:oms:events``                     - every worker report: accepted, status, modified, unknown,
  FILL, heartbeats, alerts, and the OMS's own POSITION_BOOK / HOLDINGS refreshes (free snapshots).

Streams are capped by the OMS at ~1 day, and TradePulse saves its cursor after every batch, so a
restart resumes exactly where it stopped. On the very first start the cursor is midnight IST today:
today's history is loaded and shown, but not notified (``backfill``).

Ordering: the order details arrive on a command stream and its reports on the event stream. An event
for an order not seen yet is parked (``pending_events``) and retried for up to 10 minutes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from .. import bus as busmod
from .. import events as ev
from ..config import IST, ist_iso, now_ist, today_ist
from ..processor import enqueue

log = logging.getLogger("tradepulse.redis_tail")

FORWARD_RANK = {"ROUTED": 0, "SUBMITTING": 1, "OPEN": 2, "PARTIALLY_FILLED": 3}
TERMINAL = ("COMPLETE", "REJECTED", "CANCELLED", "FAILED", "EXPIRED")
ORDER_EVENTS = {"ORDER_SUBMITTING", "ORDER_ACCEPTED", "ORDER_STATUS", "ORDER_MODIFIED", "ORDER_UNKNOWN", "FILL"}
PARK_SECONDS = 600


def ns_iso(at_ns) -> str:
    try:
        return datetime.fromtimestamp(int(at_ns) / 1e9, IST).isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OSError):
        return ist_iso()


def midnight_id() -> str:
    n = now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
    return f"{int(n.timestamp() * 1000)}-0"


def _id_ms(stream_id: str) -> int:
    try:
        return int(str(stream_id).split("-")[0])
    except ValueError:
        return 0


class RedisTail:
    def __init__(self, store, bus, keeper, events_stream="smx:oms:events", command_pattern="smx:oms:cmd:*"):
        self.store, self.bus, self.keeper = store, bus, keeper
        self.events_stream, self.command_pattern = events_stream, command_pattern
        self._streams: list[str] = []
        self._streams_at = 0.0
        self._roster_at = 0.0
        self.stats = {"ticks": 0, "messages": 0, "events": 0, "parked": 0, "errors": 0, "last_tick": "",
                      "last_message_at": "", "last_error": "", "roster_accounts": 0, "streams": 0, "redis_ok": None}

    # --- cursors ------------------------------------------------------------------------------------------
    def _cursor(self, stream: str) -> str:
        c = self.store.cursor(f"stream:{stream}")
        if not c:
            c = midnight_id()
            self.store.set_cursor(f"stream:{stream}", c)
        return c

    def _init(self) -> None:
        """On the very first start, remember each stream's last message id: everything up to it is
        today's history (shown, never notified). Streams that appear later are live from their start."""
        if self.store.cursor("redis_initialized"):
            return
        for s in [*self.discover(), self.events_stream]:
            self.store.set_cursor(f"backfill:{s}", self.bus.last_id(s))
        self.store.set_cursor("redis_initialized", ist_iso())

    def _is_backfill(self, stream: str, mid: str) -> bool:
        b = self.store.cursor(f"backfill:{stream}")
        if not b or b == "0-0":
            return False
        try:
            return tuple(int(x) for x in mid.split("-")) <= tuple(int(x) for x in b.split("-"))
        except ValueError:
            return False

    # --- roster ----------------------------------------------------------------------------------------
    def sync_roster(self) -> int:
        now = ist_iso()
        seen: dict[str, dict] = {}
        try:
            roster = json.loads(self.bus.get_any("smx:roster") or "[]")
        except (TypeError, ValueError):
            roster = []
        for r in roster if isinstance(roster, list) else []:
            if isinstance(r, dict) and r.get("account_id"):
                # Only these four fields are kept; api keys, secrets, TOTP seeds and MPINs are dropped here.
                seen[str(r["account_id"])] = {"broker": str(r.get("broker") or "").lower(), "client_name": str(r.get("name") or ""),
                                              "owner_username": str(r.get("username") or ""), "in_roster": 1}
        del roster
        try:
            keyed = json.loads(self.bus.get_any("smx:oms:apikeys") or "[]")
        except (TypeError, ValueError):
            keyed = []
        for a in keyed if isinstance(keyed, list) else []:
            seen.setdefault(str(a), {"broker": "", "client_name": "", "owner_username": "", "in_roster": 0})
        with self.store.tx() as c:
            for acct, d in seen.items():
                sess = self.bus.hgetall_any(f"smx:session:{acct}") or {}
                c.execute("""INSERT INTO accounts (account_id, broker, client_name, owner_username, in_roster, active, session_status,
                                 session_updated, first_seen, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                             ON CONFLICT(account_id) DO UPDATE SET
                               broker = CASE WHEN excluded.broker != '' THEN excluded.broker ELSE accounts.broker END,
                               client_name = CASE WHEN excluded.client_name != '' THEN excluded.client_name ELSE accounts.client_name END,
                               owner_username = CASE WHEN excluded.owner_username != '' THEN excluded.owner_username ELSE accounts.owner_username END,
                               in_roster = excluded.in_roster, active = 1, session_status = excluded.session_status,
                               session_updated = excluded.session_updated, updated_at = excluded.updated_at""",
                          (acct, d["broker"], d["client_name"], d["owner_username"], d["in_roster"],
                           str(sess.get("status") or "UNKNOWN"), str(sess.get("updated_date") or ""), now, now))
            if seen:
                c.execute(f"UPDATE accounts SET active = 0 WHERE account_id NOT IN ({','.join('?' * len(seen))})", tuple(seen))
            # owner -> TradePulse user (same username as in the OMS)
            c.execute("""UPDATE accounts SET user_id = (SELECT id FROM users WHERE users.username = accounts.owner_username COLLATE NOCASE)
                         WHERE user_id IS NULL AND owner_username != ''""")
        self.stats["roster_accounts"] = len(seen)
        return len(seen)

    # --- one pass ---------------------------------------------------------------------------------------
    def discover(self) -> list[str]:
        if time.monotonic() - self._streams_at > 30 or not self._streams:
            self._streams = sorted(set(self.bus.scan_any(self.command_pattern)))
            self._streams_at = time.monotonic()
            self.stats["streams"] = len(self._streams)
        return self._streams

    def tick(self, block_ms: int = 0) -> int:
        self._init()
        if time.monotonic() - self._roster_at > 60:
            self.sync_roster()
            self._roster_at = time.monotonic()
        n = 0
        for s in self.discover():                    # commands first: they carry the order details
            n += self._drain(s, self._on_command)
        n += self._drain(self.events_stream, self._on_event, block_ms=block_ms)
        n += self._retry_parked()
        self.stats["ticks"] += 1
        self.stats["last_tick"] = ist_iso()
        self.stats["redis_ok"] = True
        return n

    def _drain(self, stream: str, handler, block_ms: int = 0) -> int:
        n = 0
        cur = self._cursor(stream)
        while True:
            msgs = self.bus.read(stream, cur, count=500, block_ms=block_ms if not n else 0)
            block_ms = 0
            if not msgs:
                break
            for mid, fields in msgs:
                msg_type = fields.get("type", "")
                try:
                    data = json.loads(fields.get("data") or "{}")
                except (TypeError, ValueError):
                    data = {}
                try:
                    handler(mid, msg_type, data, backfill=self._is_backfill(stream, mid))
                except Exception as ex:
                    self.stats["errors"] += 1
                    self.stats["last_error"] = f"{msg_type}: {type(ex).__name__}: {ex}"[:300]
                    log.exception("stream message %s %s failed", stream, msg_type)
                cur = mid
                n += 1
            self.store.set_cursor(f"stream:{stream}", cur)
            self.stats["messages"] += len(msgs)
            self.stats["last_message_at"] = ist_iso()
            if len(msgs) < 500:
                break
        return n

    # --- commands --------------------------------------------------------------------------------------
    def _on_command(self, mid, msg_type, d, backfill=False) -> None:
        if msg_type == "EXECUTE":
            for o in d.get("orders") or []:
                self._upsert_order(o, d, mid, backfill, announce=True)
        elif msg_type == "RESUME":
            for item in d.get("orders") or []:
                o = item.get("order") if isinstance(item, dict) and "order" in item else item
                if isinstance(o, dict):
                    self._upsert_order(o, {**d, "trading_mode": o.get("trading_mode") or d.get("trading_mode")}, mid, backfill,
                                       announce=False)

    def _upsert_order(self, o: dict, group: dict, mid: str, backfill: bool, announce: bool) -> None:
        oid = str(o.get("internal_order_id") or "")
        account = str(group.get("account_id") or o.get("account_id") or "")
        if not oid or not account:
            return
        at = ns_iso(group.get("routed_ns")) if group.get("routed_ns") else ns_iso(_id_ms(mid) * 1_000_000)
        mode = str(group.get("trading_mode") or "")
        with self.store.tx() as c:
            new = c.execute("""INSERT OR IGNORE INTO orders (internal_order_id, execution_id, account_id, broker, trading_mode, strategy_id,
                                   exchange, exchange_token, symbol, broker_symbol, instrument_class, side, position_effect, product,
                                   order_type, ordered_quantity, price, status, created_at, updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ROUTED',?,?)""",
                            (oid, str(group.get("execution_id") or ""), account, str(group.get("broker") or "").lower(), mode,
                             str(group.get("strategy_id") or ""), str(o.get("exchange") or ""), int(o.get("exchange_token") or 0),
                             str(o.get("symbol") or ""), str(o.get("broker_symbol") or ""), str(o.get("instrument_class") or ""),
                             str(o.get("side") or ""), str(o.get("position_effect") or ""), str(o.get("product") or ""),
                             str(o.get("order_type") or ""), int(o.get("ordered_quantity") or 0), o.get("price"), at, at)).rowcount
            if new:
                c.execute("INSERT INTO order_events (internal_order_id, from_status, to_status, source, stream_id, at) "
                          "VALUES (?, '', 'ROUTED', 'OMS', ?, ?)", (oid, mid, at))
            if mode:
                c.execute("UPDATE accounts SET trading_mode = ? WHERE account_id = ?", (mode, account))
            c.execute("""INSERT INTO accounts (account_id, broker, first_seen, updated_at) VALUES (?, ?, ?, ?)
                         ON CONFLICT(account_id) DO NOTHING""", (account, str(group.get("broker") or "").lower(), at, at))
        if new and announce:
            self._emit(ev.ORDER_PLACED, oid, f"ord:{account}:{oid}:ORDER_PLACED:0", "ROUTED", backfill, at=at)

    # --- worker events --------------------------------------------------------------------------------
    def _on_event(self, mid, msg_type, d, backfill=False) -> None:
        if msg_type in ORDER_EVENTS and d.get("internal_order_id"):
            if not self.store.one("SELECT 1 AS x FROM orders WHERE internal_order_id = ?", (d["internal_order_id"],)):
                self._park(mid, msg_type, d)
                return
        self._apply(mid, msg_type, d, backfill)

    def _apply(self, mid, msg_type, d, backfill=False) -> None:
        at = ns_iso(d.get("at_ns")) if d.get("at_ns") else ns_iso(_id_ms(mid) * 1_000_000)
        account = str(d.get("account_id") or "")
        if msg_type == "HEARTBEAT":
            self.store.execute("""INSERT INTO workers (account_id, worker_id, status, broker, broker_connected, open_orders, last_error,
                                      last_heartbeat_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                  ON CONFLICT(account_id) DO UPDATE SET worker_id = excluded.worker_id, status = excluded.status,
                                      broker = excluded.broker, broker_connected = excluded.broker_connected,
                                      open_orders = excluded.open_orders, last_error = excluded.last_error,
                                      last_heartbeat_at = excluded.last_heartbeat_at""",
                               (account, str(d.get("worker_id") or ""), str(d.get("status") or ""), str(d.get("broker") or ""),
                                int(bool(d.get("broker_connected"))), int(d.get("open_orders") or 0),
                                str(d.get("last_error") or "")[:300], at))
            return
        if msg_type == "ORDER_SUBMITTING":
            self._transition(d["internal_order_id"], "SUBMITTING", "WORKER", mid, at, price=d.get("price"),
                             message=f"sending to broker at {d.get('price') if d.get('price') is not None else 'MARKET'}")
        elif msg_type == "ORDER_ACCEPTED":
            if self._transition(d["internal_order_id"], "OPEN", str(d.get("source") or "BROKER"), mid, at,
                                broker_order_id=str(d.get("broker_order_id") or ""),
                                message=str(d.get("message") or "accepted by the broker")):
                o = self._order(d["internal_order_id"])
                self._emit(ev.ORDER_OPEN, o["internal_order_id"],
                           ev.order_key(o["account_id"], o["broker_order_id"] or o["internal_order_id"], ev.ORDER_OPEN, 0),
                           "OPEN", backfill, at=at)
        elif msg_type == "ORDER_STATUS":
            status = str(d.get("status") or "").upper()
            filled = int(d.get("filled_quantity") or 0)
            if status == "COMPLETE" and filled == 0:
                status = "UNKNOWN"
            if self._transition(d["internal_order_id"], status, str(d.get("source") or "BROKER_POLL"), mid, at,
                                broker_order_id=str(d.get("broker_order_id") or ""), filled=filled,
                                average=d.get("average_price"), message=str(d.get("message") or "")):
                et = ev.STATUS_EVENT.get(status)
                if et:
                    o = self._order(d["internal_order_id"])
                    self._emit(et, o["internal_order_id"],
                               ev.order_key(o["account_id"], o["broker_order_id"] or o["internal_order_id"], et,
                                            o["filled_quantity"] if et in (ev.ORDER_FILLED, ev.ORDER_PARTIALLY_FILLED) else 0),
                               status, backfill, at=at, message=str(d.get("message") or ""))
        elif msg_type == "ORDER_MODIFIED":
            oid = d["internal_order_id"]
            self.store.execute("UPDATE orders SET price = ?, modification_count = modification_count + 1, updated_at = ? "
                               "WHERE internal_order_id = ? AND status NOT IN ('COMPLETE','REJECTED','CANCELLED','FAILED','EXPIRED')",
                               (d.get("price"), at, oid))
            o = self._order(oid)
            msg = f"re-priced to {d.get('price')} from LTP {d.get('ltp')} (attempt {d.get('attempt')})"
            self.store.execute("INSERT INTO order_events (internal_order_id, from_status, to_status, source, message, stream_id, at) "
                               "VALUES (?, ?, ?, 'WORKER', ?, ?, ?)", (oid, o["status"], o["status"], msg, mid, at))
            self._emit(ev.ORDER_MODIFIED, oid, f"mod:{mid}", o["status"], backfill, at=at, message=msg)
        elif msg_type == "ORDER_UNKNOWN":
            if self._transition(d["internal_order_id"], "UNKNOWN", "WORKER", mid, at, message=str(d.get("message") or "")):
                o = self._order(d["internal_order_id"])
                self._emit(ev.ORDER_UNKNOWN, o["internal_order_id"],
                           ev.order_key(o["account_id"], o["broker_order_id"] or o["internal_order_id"], ev.ORDER_UNKNOWN, 0),
                           "UNKNOWN", backfill, at=at, message=str(d.get("message") or ""))
        elif msg_type == "FILL":
            self._fill(mid, d, at, backfill)
        elif msg_type == "GROUP_REJECTED":
            reason = f"{d.get('code') or 'WORKER_REJECTED'}: {d.get('reason') or ''}"
            for o in self.store.rows("SELECT internal_order_id FROM orders WHERE execution_id = ?", (str(d.get("execution_id") or ""),)):
                if self._transition(o["internal_order_id"], "REJECTED", "WORKER", mid, at, message=reason):
                    oo = self._order(o["internal_order_id"])
                    self._emit(ev.ORDER_REJECTED, oo["internal_order_id"],
                               ev.order_key(oo["account_id"], oo["internal_order_id"], ev.ORDER_REJECTED, 0), "REJECTED",
                               backfill, at=at, message=reason)
        elif msg_type == "ALERT":
            level = str(d.get("level") or "warning").upper()
            if level in ("ERROR", "WARNING") and self.store.setting("track_alerts"):
                e = ev.new_event(ev.ACCOUNT_ALERT, "OMS", f"walert:{mid}", account_id=account,
                                 internal_order_id=str(d.get("internal_order_id") or ""), message=str(d.get("message") or "")[:500],
                                 payload={"kind": "WORKER_ALERT", "severity": "CRITICAL" if level == "ERROR" else "WARNING"},
                                 occurred_at=at)
                self._enqueue(e, backfill)
        elif msg_type in ("POSITION_BOOK", "HOLDINGS") and account:
            if d.get("error"):
                return
            # The OMS publishes a book per trading mode. Only the LIVE book is the broker's real book; a PAPER
            # book of a real broker account must never become its baseline (it caused false mismatches).
            broker = (self.store.one("SELECT broker FROM accounts WHERE account_id = ?", (account,)) or {}).get("broker", "")
            mode = str(d.get("trading_mode") or "LIVE").upper()
            if (str(broker).lower() == "paper") != (mode == "PAPER"):
                return
            kind = "positions" if msg_type == "POSITION_BOOK" else "holdings"
            rows = [self._norm_snapshot_row(r, kind) for r in (d.get(kind if kind == "holdings" else "positions") or [])]
            self.keeper.apply_snapshot(account, kind, rows, "OMS_WORKER", at=at, compare=not backfill)

    @staticmethod
    def _norm_snapshot_row(r: dict, kind: str) -> dict:
        if kind == "positions":
            return {"exchange": r.get("exchange", ""), "exchange_token": int(r.get("exchange_token") or 0),
                    "product": str(r.get("product") or "").upper(), "net_quantity": int(r.get("net_quantity") or 0),
                    "broker_symbol": r.get("broker_symbol") or "", "average_price": float(r.get("average_price") or 0),
                    "last_price": float(r.get("last_price") or 0), "pnl": float(r.get("pnl") or 0)}
        return {"exchange": r.get("exchange", ""), "exchange_token": int(r.get("exchange_token") or 0),
                "product": str(r.get("product") or "CNC").upper(), "broker_symbol": r.get("broker_symbol") or "",
                "isin": r.get("isin") or "", "quantity": int(r.get("quantity") or 0), "sellable": int(r.get("sellable") or 0),
                "t1_quantity": int(r.get("t1_quantity") or 0), "pledged_quantity": int(r.get("pledged_quantity") or 0),
                "average_price": float(r.get("average_price") or 0), "last_price": float(r.get("last_price") or 0),
                "pnl": float(r.get("pnl") or 0)}

    def _fill(self, mid, d, at, backfill) -> None:
        o = self._order(d["internal_order_id"])
        filled_at = str(d.get("filled_at") or "") or at
        if "T" not in filled_at:
            filled_at = filled_at.replace(" ", "T")
        if "+" not in filled_at[10:] and not filled_at.endswith("Z"):
            filled_at += "+05:30"
        new = self.store.execute("""INSERT OR IGNORE INTO fills (fill_id, account_id, internal_order_id, broker_order_id, exchange_trade_id,
                                        exchange, exchange_token, symbol, product, side, quantity, price, filled_at, created_at)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (str(d["fill_id"]), o["account_id"], o["internal_order_id"], str(d.get("broker_order_id") or o["broker_order_id"]),
                                  str(d.get("exchange_trade_id") or ""), o["exchange"], o["exchange_token"], o["symbol"] or o["broker_symbol"],
                                  o["product"], o["side"], int(d.get("quantity") or 0), float(d.get("price") or 0), filled_at, ist_iso()))
        if not new:
            return
        e = ev.new_event(ev.TRADE_EXECUTED, "OMS", f"fill:{o['account_id']}:{d['fill_id']}", account_id=o["account_id"],
                         broker=o["broker"], trading_mode=o["trading_mode"], symbol=o["symbol"], exchange=o["exchange"],
                         side=o["side"], order_type=o["order_type"], product=o["product"], quantity=int(d.get("quantity") or 0),
                         filled_quantity=o["filled_quantity"], price=d.get("price"), status="EXECUTED",
                         order_id=str(d.get("broker_order_id") or o["broker_order_id"]), internal_order_id=o["internal_order_id"],
                         trade_id=str(d.get("exchange_trade_id") or d["fill_id"]), payload={"fill_id": d["fill_id"]},
                         occurred_at=filled_at)
        self._enqueue(e, backfill)
        cause = {"fill_id": str(d["fill_id"]), "exchange": o["exchange"], "exchange_token": o["exchange_token"],
                 "product": o["product"], "side": o["side"], "quantity": int(d.get("quantity") or 0), "price": d.get("price"),
                 "symbol": o["symbol"], "broker_order_id": str(d.get("broker_order_id") or o["broker_order_id"]),
                 "internal_order_id": o["internal_order_id"], "filled_at": filled_at}
        self.keeper.rebuild(o["account_id"], cause=cause, backfill=backfill)

    # --- helpers ------------------------------------------------------------------------------------
    def _order(self, oid: str) -> dict:
        return self.store.one("SELECT * FROM orders WHERE internal_order_id = ?", (oid,))

    def _transition(self, oid, status, source, mid, at, broker_order_id="", filled=None, average=None, price=None, message="") -> bool:
        """Apply a status change the way the OMS does: terminal is final, working states only move forward."""
        o = self._order(oid)
        if not o:
            return False
        cur = o["status"]
        if cur in TERMINAL:
            return False
        if status in FORWARD_RANK and cur in FORWARD_RANK and FORWARD_RANK[status] < FORWARD_RANK[cur]:
            status = cur
        filled_q = max(int(o["filled_quantity"] or 0), int(filled or 0)) if filled is not None else int(o["filled_quantity"] or 0)
        changed = status != cur or filled_q != o["filled_quantity"]
        with self.store.tx() as c:
            c.execute("""UPDATE orders SET status = ?, filled_quantity = ?, average_price = COALESCE(?, average_price),
                             broker_order_id = CASE WHEN ? != '' THEN ? ELSE broker_order_id END,
                             status_message = CASE WHEN ? != '' THEN ? ELSE status_message END,
                             price = COALESCE(price, ?), updated_at = ? WHERE internal_order_id = ?""",
                      (status, filled_q, average if average not in (None, "", 0) else None, broker_order_id, broker_order_id,
                       message[:500], message[:500], price, at, oid))
            if changed:
                c.execute("""INSERT INTO order_events (internal_order_id, from_status, to_status, source, filled_quantity, average_price,
                                 message, stream_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (oid, cur, status, source, filled_q, float(average or 0), message[:500], mid, at))
        return changed

    def _emit(self, et, oid, key, status, backfill, at=None, message="") -> None:
        o = self._order(oid)
        e = ev.new_event(et, "OMS", key, account_id=o["account_id"], broker=o["broker"], trading_mode=o["trading_mode"],
                         symbol=o["symbol"], exchange=o["exchange"], side=o["side"], order_type=o["order_type"],
                         product=o["product"], quantity=o["ordered_quantity"], filled_quantity=o["filled_quantity"],
                         price=o["price"], average_price=o["average_price"] or None, status=status,
                         order_id=o["broker_order_id"], internal_order_id=oid, message=(message or o["status_message"] or "")[:500],
                         payload={"strategy_id": o["strategy_id"]}, occurred_at=at or ist_iso())
        self._enqueue(e, backfill)

    def _enqueue(self, e: dict, backfill: bool) -> None:
        if backfill:
            e["backfill"] = True
        enqueue(self.bus, e)
        self.stats["events"] += 1

    def _park(self, mid, msg_type, d) -> None:
        self.store.execute("INSERT OR IGNORE INTO pending_events (stream_id, msg_type, data, first_seen) VALUES (?, ?, ?, ?)",
                           (mid, msg_type, json.dumps(d, default=str), ist_iso()))
        self.stats["parked"] += 1

    def _retry_parked(self) -> int:
        n = 0
        for p in self.store.rows("SELECT * FROM pending_events ORDER BY id LIMIT 200"):
            d = json.loads(p["data"])
            if self.store.one("SELECT 1 AS x FROM orders WHERE internal_order_id = ?", (d.get("internal_order_id"),)):
                self.store.execute("DELETE FROM pending_events WHERE id = ?", (p["id"],))
                try:
                    self._apply(p["stream_id"], p["msg_type"], d, backfill=self._is_backfill(self.events_stream, p["stream_id"]))
                except Exception:
                    log.exception("parked event failed")
                n += 1
            elif (now_ist() - datetime.fromisoformat(p["first_seen"])).total_seconds() > PARK_SECONDS:
                self.store.execute("DELETE FROM pending_events WHERE id = ?", (p["id"],))
                log.warning("dropped %s for unknown order %s after %ss", p["msg_type"], d.get("internal_order_id"), PARK_SECONDS)
            else:
                self.store.execute("UPDATE pending_events SET attempts = attempts + 1 WHERE id = ?", (p["id"],))
        return n

    # --- loop -------------------------------------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        log.info("Redis tail started (OMS streams, read-only)")
        while not stop.is_set():
            try:
                block = max(250, int(self.store.setting("oms_poll_interval_ms") or 1000))
                self.tick(block_ms=block)
            except Exception as ex:
                self.stats["errors"] += 1
                self.stats["redis_ok"] = False
                self.stats["last_error"] = f"{type(ex).__name__}: {ex}"[:300]
                log.warning("Redis tail error: %s", ex)
                stop.wait(3)
            try:
                self.bus.hset(busmod.HEALTH, "redis_tail", json.dumps(self.stats))
            except Exception:
                pass


__all__ = ["RedisTail", "today_ist"]
