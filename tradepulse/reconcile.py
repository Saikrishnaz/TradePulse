"""Positions & holdings reconciliation.

* **09:00 and 09:05 IST** (trading days, configurable): a snapshot of positions and holdings for every
  *monitored* account becomes the day's baseline. 09:00 also reports what changed overnight versus the
  book; 09:05 verifies the book (09:00 baseline + every fill since) against the broker.
* **Between snapshots the book is event-based**: each FILL from the OMS stream rebuilds that account's
  book (baseline ⊕ fills after it) - no broker call.
* **Free snapshots**: when the OMS's own workers publish ``POSITION_BOOK`` / ``HOLDINGS`` on the stream
  (the OMS's 15:40/16:15 books or a manual refresh in the OMS), they are compared and used as a newer
  baseline - again no broker call by TradePulse.
* **On request**: a user may refresh one account's positions or holdings; it takes the single global
  broker slot (1 request / 3 s) or is refused with "please wait N s".

Any quantity difference between the book and a snapshot is stored (``reconcile_diffs``) and raised as
a ``RECONCILE_MISMATCH`` event (notifiable like any other event).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from . import book as bk
from . import events as ev
from .brokers import BrokerReadError, Busy
from .config import IST, ist_iso, ist_iso_ms, now_ist, parse_ts, today_ist
from .processor import enqueue

log = logging.getLogger("tradepulse.reconcile")
KINDS = ("positions", "holdings")


class BookKeeper:
    def __init__(self, store, bus):
        self.store, self.bus = store, bus
        self._lock = threading.RLock()

    # --- derive -------------------------------------------------------------------------------------------
    def _baseline(self, account_id: str, kind: str) -> tuple[list[dict], str, str]:
        b = self.store.one("SELECT * FROM book_baselines WHERE account_id = ? AND kind = ?", (account_id, kind))
        if not b or not b.get("snapshot_id"):
            return [], "", ""
        s = self.store.one("SELECT payload FROM snapshots WHERE id = ?", (b["snapshot_id"],))
        return (json.loads(s["payload"]) if s else []), b["at"], b["source"]

    def _fills_after(self, account_id: str, since: str) -> list[dict]:
        """Fills strictly after ``since`` (the moment the snapshot request was SENT), compared as datetimes -
        never as strings, whose precision/offset formats differ. With no baseline: today's fills."""
        cutoff = parse_ts(since) or parse_ts(f"{today_ist()}T00:00:00+05:30")
        day_start = (cutoff.astimezone(IST).date().isoformat())
        rows = self.store.rows("""SELECT f.*, COALESCE(o.instrument_class, '') AS instrument_class FROM fills f
                                    LEFT JOIN orders o ON o.internal_order_id = f.internal_order_id
                                   WHERE f.account_id = ? AND substr(f.filled_at, 1, 10) >= ? AND f.exchange != ''""",
                               (account_id, day_start))
        out = [r for r in rows if (parse_ts(r["filled_at"]) or cutoff) > cutoff]
        out.sort(key=lambda r: (parse_ts(r["filled_at"]), r["id"]))
        return out

    def rebuild(self, account_id: str, cause: dict | None = None, emit: bool = True, backfill: bool = False) -> list[dict]:
        """Recompute the account's book. Returns the change events it emitted (position/holding)."""
        with self._lock:
            out = []
            now = ist_iso()
            for kind in KINDS:
                base, base_at, base_src = self._baseline(account_id, kind)
                fills = self._fills_after(account_id, base_at)
                table = "book_positions" if kind == "positions" else "book_holdings"
                before = {bk.pos_key(r): r for r in self.store.rows(f"SELECT * FROM {table} WHERE account_id = ?", (account_id,))}
                if kind == "positions":
                    book, actions = bk.derive_positions(base, fills)
                else:
                    book, actions = bk.derive_holdings(base, fills)
                with self.store.tx() as c:
                    c.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
                    for k, r in book.items():
                        if kind == "positions":
                            c.execute("""INSERT INTO book_positions (account_id, exchange, exchange_token, product, symbol, instrument_class,
                                         net_quantity, average_price, realized, day_buy_qty, day_sell_qty, last_price, broker_pnl,
                                         baseline_qty, baseline_at, baseline_source, fills_applied, updated_at)
                                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                      (account_id, *k, r["symbol"], r["instrument_class"], r["net_quantity"], r["average_price"],
                                       r["realized"], r["day_buy_qty"], r["day_sell_qty"], r["last_price"], r["broker_pnl"],
                                       r["baseline_qty"], base_at, base_src, r["fills_applied"], now))
                        else:
                            c.execute("""INSERT INTO book_holdings (account_id, exchange, exchange_token, product, symbol, isin, quantity,
                                         sellable, t1_quantity, pledged_quantity, average_price, last_price, baseline_qty, baseline_at,
                                         baseline_source, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                      (account_id, *k, r["symbol"], r["isin"], r["quantity"], r["sellable"], r["t1_quantity"],
                                       r["pledged_quantity"], r["average_price"], r["last_price"], r["baseline_qty"], base_at,
                                       base_src, now))
                if not emit or cause is None:
                    continue
                qty_field = "net_quantity" if kind == "positions" else "quantity"
                for k, action in actions.items():
                    if not (k == bk.pos_key(cause) or (kind == "holdings" and k[:2] == bk.pos_key(cause)[:2])):
                        continue                        # only the instrument this fill touched
                    prev = int((before.get(k) or {}).get(qty_field) or 0)
                    cur = int(book[k][qty_field])
                    if prev == cur:
                        continue
                    e = self._change_event(account_id, kind, book[k], prev, cur, action, cause)
                    if backfill:
                        e["backfill"] = True
                    enqueue(self.bus, e)
                    out.append(e)
            return out

    def _change_event(self, account_id, kind, r, prev, cur, action, cause) -> dict:
        et = ev.POSITION_UPDATED if kind == "positions" else ev.HOLDING_UPDATED
        return ev.new_event(
            et, "BOOK", f"book:{kind}:{account_id}:{':'.join(map(str, bk.pos_key(r)))}:{cause.get('fill_id')}",
            account_id=account_id, symbol=r.get("symbol") or cause.get("symbol") or "", exchange=r["exchange"],
            side="LONG" if cur > 0 else "SHORT" if cur < 0 else (cause.get("side") or ""), product=r["product"],
            quantity=cur, filled_quantity=int(cause.get("quantity") or 0), price=cause.get("price"),
            average_price=float(r.get("average_price") or 0) or None, status=action,
            order_id=cause.get("broker_order_id") or "", internal_order_id=cause.get("internal_order_id") or "",
            message=f"{kind[:-1]} {prev} → {cur}",
            payload={"kind": kind, "previous": prev, "current": cur, "fill_id": cause.get("fill_id"),
                     "realized": r.get("realized"), "t1_quantity": r.get("t1_quantity")},
            occurred_at=cause.get("filled_at") or ist_iso())

    # --- snapshots -------------------------------------------------------------------------------------------
    def apply_snapshot(self, account_id: str, kind: str, rows: list[dict], source: str, at: str | None = None,
                       run_id: int | None = None, compare: bool = True) -> list[dict]:
        """Store a snapshot, diff it against the book, make it the new baseline, rebuild. Returns diffs."""
        at = at or ist_iso()
        with self._lock:
            table = "book_positions" if kind == "positions" else "book_holdings"
            qty_field = "net_quantity" if kind == "positions" else "quantity"
            current = {bk.pos_key(r): r for r in self.store.rows(f"SELECT * FROM {table} WHERE account_id = ?", (account_id,))}
            had_book = bool(self.store.one("SELECT 1 AS x FROM book_baselines WHERE account_id = ? AND kind = ?",
                                           (account_id, kind))) or bool(current)
            # The book includes fills up to now; a fill newer than the snapshot is not in the snapshot.
            later = [f for f in self._fills_after(account_id, at)]
            if kind == "holdings":
                later = [f for f in later if str(f.get("product") or "").upper() in bk.DELIVERY]
            snap_view = rows
            if later:
                derive = bk.derive_positions if kind == "positions" else bk.derive_holdings
                d, _ = derive(rows, later)
                snap_view = list(d.values())
                if kind == "holdings":
                    snap_view = [{**r, "broker_symbol": r["symbol"]} for r in snap_view]
            diffs = bk.compare(current, snap_view, qty_field, "CNC" if kind == "holdings" else "") if compare and had_book else []
            with self.store.tx() as c:
                sid = c.execute("""INSERT INTO snapshots (account_id, kind, source, ok, rows, payload, run_id, at)
                                   VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
                                (account_id, kind, source, len(rows), json.dumps(rows, default=str), run_id, at)).lastrowid
                c.execute("""INSERT INTO book_baselines (account_id, kind, at, source, rows, snapshot_id) VALUES (?, ?, ?, ?, ?, ?)
                             ON CONFLICT(account_id, kind) DO UPDATE SET at = excluded.at, source = excluded.source,
                             rows = excluded.rows, snapshot_id = excluded.snapshot_id""",
                          (account_id, kind, at, source, len(rows), sid))
                for d in diffs:
                    c.execute("""INSERT INTO reconcile_diffs (run_id, account_id, kind, exchange, exchange_token, product, symbol,
                                 book_qty, broker_qty, at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                              (run_id or 0, account_id, kind, d["exchange"], d["exchange_token"], d["product"], d["symbol"],
                               d["book_qty"], d["broker_qty"], at))
            self.rebuild(account_id, emit=False)
            for d in diffs:
                enqueue(self.bus, ev.new_event(
                    ev.RECONCILE_MISMATCH, "RECONCILE",
                    f"diff:{account_id}:{kind}:{d['exchange']}:{d['exchange_token']}:{d['product']}:{d['book_qty']}:{d['broker_qty']}:{at}",
                    account_id=account_id, symbol=d["symbol"], exchange=d["exchange"], product=d["product"],
                    quantity=d["broker_qty"], status="MISMATCH",
                    message=f"{kind}: TradePulse book {d['book_qty']} vs broker {d['broker_qty']} ({source})",
                    payload={"kind": kind, "book_qty": d["book_qty"], "broker_qty": d["broker_qty"], "source": source,
                             "severity": "WARNING"}, occurred_at=at))
            return diffs

    def record_failure(self, account_id: str, kind: str, source: str, error: str, run_id: int | None = None) -> None:
        self.store.execute("INSERT INTO snapshots (account_id, kind, source, ok, error, run_id, at) VALUES (?, ?, ?, 0, ?, ?, ?)",
                           (account_id, kind, source, error[:500], run_id, ist_iso()))


class Reconciler:
    def __init__(self, store, bus, readers, keeper: BookKeeper, data):
        self.store, self.bus, self.readers, self.keeper, self.data = store, bus, readers, keeper, data
        self._run_lock = threading.Lock()
        self.current: dict | None = None            # progress of the running pass, for the dashboard

    def accounts_for_run(self) -> list[dict]:
        mon = self.store.monitored_ids()
        return [a for a in self.data.accounts() if a["account_id"] in mon and a["account_status"] == "ACTIVE"]

    # --- scheduled / full pass ------------------------------------------------------------------------------
    def run(self, trigger: str, stop: threading.Event | None = None, account_ids: list[str] | None = None) -> dict:
        if not self._run_lock.acquire(blocking=False):
            return {"status": "ALREADY_RUNNING"}
        try:
            accts = self.accounts_for_run()
            if account_ids is not None:
                accts = [a for a in accts if a["account_id"] in account_ids]
            run_id = self.store.execute("INSERT INTO reconcile_runs (trigger, accounts, started_at) VALUES (?, ?, ?)",
                                        (trigger, len(accts), ist_iso()))
            stats = {"requests": 0, "snapshots": 0, "errors": 0, "mismatches": 0}
            total = len(accts) * len(KINDS)
            self.current = {"run_id": run_id, "trigger": trigger, "done": 0, "total": total, "started_at": ist_iso()}
            for a in accts:
                for kind in KINDS:
                    if stop is not None and stop.is_set():
                        break
                    try:
                        if self.readers.needs_slot(a["broker"]):
                            self.readers.wait_slot(stop)
                            stats["requests"] += 1
                        sent_at = ist_iso_ms()                 # the snapshot reflects fills before this moment
                        rows = self.readers.fetch(a["account_id"], a["broker"], kind)
                        diffs = self.keeper.apply_snapshot(a["account_id"], kind, rows, f"BROKER {trigger}", at=sent_at,
                                                           run_id=run_id)
                        stats["snapshots"] += 1
                        stats["mismatches"] += len(diffs)
                    except BrokerReadError as ex:
                        stats["errors"] += 1
                        self.keeper.record_failure(a["account_id"], kind, f"BROKER {trigger}", str(ex), run_id)
                        log.warning("reconcile %s %s %s: %s", trigger, a["account_id"], kind, ex)
                    self.current["done"] += 1
            status = "DONE" if not stats["errors"] else ("PARTIAL" if stats["snapshots"] else "FAILED")
            self.store.execute("""UPDATE reconcile_runs SET status = ?, requests = ?, snapshots = ?, errors = ?, mismatches = ?,
                                  finished_at = ? WHERE id = ?""",
                               (status, stats["requests"], stats["snapshots"], stats["errors"], stats["mismatches"], ist_iso(), run_id))
            return {"run_id": run_id, "status": status, **stats}
        finally:
            self.current = None
            self._run_lock.release()

    # --- user request: one account, one kind, one slot ----------------------------------------------------
    def request(self, account_id: str, kind: str, actor: str) -> dict:
        if kind not in KINDS:
            raise ValueError("kind must be positions or holdings")
        a = self.data.account(account_id)
        if not a:
            raise LookupError("account not found")
        if self.readers.needs_slot(a["broker"]):
            self.readers.try_slot()                         # raises Busy(wait_ms) -> "please wait"
        run_id = self.store.execute("INSERT INTO reconcile_runs (trigger, accounts, requests, started_at) VALUES (?, 1, ?, ?)",
                                    (f"USER:{actor} {kind}", 1 if self.readers.needs_slot(a["broker"]) else 0, ist_iso()))
        sent_at = ist_iso_ms()
        try:
            rows = self.readers.fetch(account_id, a["broker"], kind)
        except BrokerReadError as ex:
            self.keeper.record_failure(account_id, kind, f"USER:{actor}", str(ex), run_id)
            self.store.execute("UPDATE reconcile_runs SET status = 'FAILED', errors = 1, finished_at = ?, detail = ? WHERE id = ?",
                               (ist_iso(), str(ex)[:300], run_id))
            raise
        diffs = self.keeper.apply_snapshot(account_id, kind, rows, f"USER:{actor}", at=sent_at, run_id=run_id)
        self.store.execute("UPDATE reconcile_runs SET status = 'DONE', snapshots = 1, mismatches = ?, finished_at = ? WHERE id = ?",
                           (len(diffs), ist_iso(), run_id))
        return {"run_id": run_id, "rows": len(rows), "mismatches": diffs}

    # --- schedule -------------------------------------------------------------------------------------------
    def due(self, now: datetime | None = None) -> list[str]:
        now = now or now_ist()
        s = self.store.get_settings()
        if not s.get("reconcile_enabled", True) or now.weekday() not in s.get("trading_days", [0, 1, 2, 3, 4]):
            return []
        out = []
        for t in s.get("reconcile_times") or []:
            try:
                hh, mm = (int(x) for x in str(t).split(":"))
            except ValueError:
                continue
            at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if not (at <= now and (now - at).total_seconds() < 1800):
                continue
            r = self.store.one("SELECT last_at FROM schedule_state WHERE name = ?", (f"reconcile:{t}",))
            if r and datetime.fromisoformat(r["last_at"]) >= at:
                continue
            out.append(t)
        return out

    def run_due(self, stop: threading.Event | None = None, now: datetime | None = None) -> list[dict]:
        results = []
        for t in self.due(now):
            stamp = (now or now_ist()).astimezone(IST).isoformat(timespec="seconds")
            self.store.execute("""INSERT INTO schedule_state (name, last_at) VALUES (?, ?)
                                  ON CONFLICT(name) DO UPDATE SET last_at = excluded.last_at""", (f"reconcile:{t}", stamp))
            results.append(self.run(f"SCHEDULED {t}", stop))
        return results

    def loop(self, stop: threading.Event) -> None:
        log.info("reconciler started")
        while not stop.is_set():
            try:
                self.run_due(stop)
            except Exception:
                log.exception("reconcile loop error")
            stop.wait(15)


__all__ = ["BookKeeper", "Reconciler", "Busy"]
