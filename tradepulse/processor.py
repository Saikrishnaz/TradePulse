"""Event processor: ingest stream -> dedupe -> enrich -> store -> outbox -> live fan-out.

Exactly-once *effects* on top of at-least-once delivery:
the event row (``UNIQUE dedupe_key``) and its notification rows (``UNIQUE event_id, channel_id``)
are written in one SQLite transaction, and the stream message is acknowledged only after that
commits. A crash before the ack means the message is re-claimed and re-processed; the unique
key turns the second attempt into a no-op. Duplicate broker webhook deliveries are dropped the
same way.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta

from . import bus as busmod
from .config import IST, ist_iso, now_ist
from .notify.format import event_text
from .rules import FINAL_TYPES, RuleSnapshot, final_only, targets

FINAL_SETTLE_SECONDS = 3            # wait for fills reported right after the final status

log = logging.getLogger("tradepulse.processor")


def enqueue(bus, event: dict) -> str:
    """What every source calls: durable hand-off, returns fast."""
    return bus.add(busmod.INGEST, event)


class Processor:
    def __init__(self, store, bus, oms, consumer: str = "proc-1"):
        self.store, self.bus, self.oms, self.consumer = store, bus, oms, consumer
        self.bus.ensure_group(busmod.INGEST, busmod.INGEST_GROUP)
        self._snap: RuleSnapshot | None = None
        self._snap_at = 0.0
        self._accounts: dict[str, dict] = {}
        self._accounts_at = 0.0
        self.stats = {"processed": 0, "duplicates": 0, "failed": 0, "last_at": ""}

    # --- caches -------------------------------------------------------------------------
    def invalidate(self) -> None:
        self._snap_at = 0.0

    def _account_map(self) -> dict:
        if time.monotonic() - self._accounts_at > 60 or not self._accounts:
            try:
                self._accounts = {a["account_id"]: a for a in self.oms.accounts()}
                self._accounts_at = time.monotonic()
            except Exception as ex:                      # OMS DB briefly unavailable: keep last map
                log.warning("account map refresh failed: %s", ex)
        return self._accounts

    def snapshot(self) -> RuleSnapshot:
        if self._snap is None or time.monotonic() - self._snap_at > 3:
            client_of = {a: r["user_id"] for a, r in self._account_map().items()}
            self._snap = RuleSnapshot.load(self.store, client_of)
            self._snap_at = time.monotonic()
        return self._snap

    # --- loop ---------------------------------------------------------------------------
    def run_once(self, block_ms: int = 1000) -> int:
        done = 0
        batch = self.bus.claim_stale(busmod.INGEST, busmod.INGEST_GROUP, self.consumer, min_idle_ms=30000)
        batch += self.bus.read_group(busmod.INGEST, busmod.INGEST_GROUP, self.consumer, count=100,
                                     block_ms=0 if batch else block_ms)
        for mid, event in batch:
            try:
                self.process(event)
                self.bus.ack(busmod.INGEST, busmod.INGEST_GROUP, mid)
                done += 1
            except Exception:
                # Left unacknowledged: re-claimed after 30s. A poison message is acked after logging
                # only if it is structurally invalid (no type), so it cannot block the queue forever.
                self.stats["failed"] += 1
                log.exception("event processing failed (%s)", event.get("type"))
                if not event.get("type") or not event.get("dedupe_key"):
                    self.bus.ack(busmod.INGEST, busmod.INGEST_GROUP, mid)
        if done:
            self._health()
        return done

    def run(self, stop: threading.Event) -> None:
        log.info("processor started (%s)", self.consumer)
        while not stop.is_set():
            try:
                self.run_once(block_ms=1000)
            except Exception:
                log.exception("processor loop error")
                stop.wait(1.0)

    # --- one event ----------------------------------------------------------------------
    def enrich(self, e: dict) -> dict:
        accounts = self._account_map()
        if not e.get("account_id") and e.get("order_id"):
            e["account_id"] = self.oms.account_by_broker_order_id(e.get("broker", ""), e["order_id"]) or ""
        a = accounts.get(e.get("account_id") or "")
        if a:
            e["client_name"] = e.get("client_name") or a.get("client_name") or ""
            e["broker"] = e.get("broker") or a.get("broker") or ""
            e["trading_mode"] = e.get("trading_mode") or a.get("trading_mode") or ""
        return e

    def process(self, e: dict) -> bool:
        """Returns True when stored as new, False when it was a duplicate."""
        e = self.enrich(dict(e))
        snap = self.snapshot()
        # A first-start backfill of today's history is stored and shown, never sent.
        tgts = [] if e.get("backfill") else targets(snap, e)
        now = ist_iso()
        with self.store.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO events (event_id, dedupe_key, type, source, account_id, client_name, broker,
                       trading_mode, symbol, exchange, side, order_type, product, quantity, filled_quantity, price,
                       average_price, status, order_id, internal_order_id, trade_id, message, payload, occurred_at,
                       received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (e["event_id"], e["dedupe_key"], e["type"], e["source"], e.get("account_id") or "",
                 e.get("client_name") or "", e.get("broker") or "", e.get("trading_mode") or "", e.get("symbol") or "",
                 e.get("exchange") or "", e.get("side") or "", e.get("order_type") or "", e.get("product") or "",
                 int(e.get("quantity") or 0), int(e.get("filled_quantity") or 0), e.get("price"), e.get("average_price"),
                 e.get("status") or "", e.get("order_id") or "", e.get("internal_order_id") or "",
                 e.get("trade_id") or "", (e.get("message") or "")[:1000],
                 json.dumps(e.get("payload") or {}, default=str), e.get("occurred_at") or now, now))
            if not cur.rowcount:
                self.stats["duplicates"] += 1
                return False
            text = event_text(e) if tgts else ""
            # One message per order: the final state is summarised when it is sent, a few seconds later, so
            # fills reported just after the status change are included.
            final = bool(tgts) and final_only(snap) and e.get("type") in FINAL_TYPES and bool(e.get("internal_order_id"))
            kind = "order_final" if final else "event"
            for t in tgts:
                g = t["group"]
                batched = g.get("delivery_mode") == "batched"
                due = next_batch_boundary(int(g.get("batch_minutes") or 5)) if batched else (
                    ist_iso(now_ist() + timedelta(seconds=FINAL_SETTLE_SECONDS)) if final else now)
                c.execute("""INSERT OR IGNORE INTO notifications (event_id, kind, channel_id, group_id, text, status,
                                 batched, next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?)""",
                          (e["event_id"], kind, t["channel"]["id"], g["id"], text, int(batched), due, now))
        self.stats["processed"] += 1
        self.stats["last_at"] = now
        try:
            live = {k: e.get(k) for k in ("event_id", "type", "source", "account_id", "client_name", "broker",
                                          "trading_mode", "symbol", "exchange", "side", "order_type", "quantity",
                                          "filled_quantity", "price", "average_price", "status", "order_id",
                                          "internal_order_id", "trade_id", "message", "occurred_at")}
            live["notified"] = len(tgts)
            self.bus.add(busmod.LIVE, live, maxlen=5000)
        except Exception:
            log.warning("live fan-out failed (event stored)")
        return True

    def _health(self) -> None:
        try:
            self.bus.hset(busmod.HEALTH, "processor", json.dumps({**self.stats, "at": ist_iso()}))
        except Exception:
            pass


def next_batch_boundary(minutes: int) -> str:
    minutes = max(1, int(minutes))
    n = now_ist()
    start = n.replace(second=0, microsecond=0)
    m = (start.minute // minutes + 1) * minutes
    boundary = start.replace(minute=0) + timedelta(minutes=m)
    return boundary.astimezone(IST).isoformat(timespec="seconds")


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)
