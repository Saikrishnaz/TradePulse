"""TradePulse's data access over its OWN database - same interface the dashboard and bot
used against the OMS before, so they are unchanged. Everything here was learnt from Redis (the OMS's
streams and roster) or a read-only broker snapshot; the OMS itself is never contacted, so all of it
keeps working after the OMS host goes down for the day.

Visibility (same rules the OMS uses): ADMIN sees everything; a MANAGER sees their own accounts and
those of users whose ``manager_id`` is them; a CLIENT sees the accounts they own (``accounts.user_id``).
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

from . import book as bk
from .config import today_ist

TERMINAL = ("COMPLETE", "REJECTED", "CANCELLED", "FAILED", "EXPIRED")
WORKING = ("RECEIVED", "VALIDATED", "ROUTED", "SUBMITTING", "OPEN", "PARTIALLY_FILLED", "UNKNOWN")
D0 = Decimal("0")


def mask_account(account_id: str) -> str:
    """``****1234`` - a fixed-width mask, so not even the id's length is revealed."""
    s = str(account_id or "")
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    return "****" + s[-4:]


def _in(col: str, ids: list[str]) -> tuple[str, tuple]:
    return f"{col} IN ({','.join('?' * len(ids))})", tuple(ids)


class LocalData:
    def __init__(self, store):
        self.store = store

    # compatibility
    @contextmanager
    def conn(self):
        with self.store.conn() as c:
            yield c

    def available(self) -> bool:
        return True

    def _rows(self, sql, params=()):
        return self.store.rows(sql, params)

    def _one(self, sql, params=()):
        return self.store.one(sql, params)

    # --- users / scoping ---------------------------------------------------------------------------
    def user_by_username(self, username: str) -> dict | None:
        return self._one("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (str(username).strip(),))

    def user_by_id(self, user_id: int) -> dict | None:
        return self._one("SELECT id, username, name, role, status, manager_id FROM users WHERE id = ?", (user_id,))

    def users(self) -> list[dict]:
        return self._rows("SELECT id, username, name, role, status, manager_id, source, created_at FROM users ORDER BY name")

    def visible_user_ids(self, user: dict) -> list[int] | None:
        if user.get("role") == "ADMIN":
            return None
        if user.get("role") == "MANAGER":
            return [r["id"] for r in self._rows("SELECT id FROM users WHERE manager_id = ? OR id = ?", (user["id"], user["id"]))]
        return [user["id"]]

    def visible_account_ids(self, user: dict) -> list[str]:
        ids = self.visible_user_ids(user)
        if ids is None:
            return [r["account_id"] for r in self._rows("SELECT account_id FROM accounts ORDER BY account_id")]
        if not ids:
            return []
        w, p = _in("user_id", ids)
        return [r["account_id"] for r in self._rows(f"SELECT account_id FROM accounts WHERE {w} ORDER BY account_id", p)]

    # --- accounts -----------------------------------------------------------------------------------------
    def accounts(self, account_ids: list[str] | None = None) -> list[dict]:
        sql = """SELECT a.account_id, a.broker, CASE WHEN a.active = 1 THEN 'ACTIVE' ELSE 'INACTIVE' END AS account_status,
                        'AUTO' AS auth_method, a.user_id, COALESCE(u.username, a.owner_username) AS username,
                        COALESCE(NULLIF(a.client_name, ''), u.name, a.account_id) AS client_name,
                        COALESCE(u.role, 'CLIENT') AS role, COALESCE(u.status, 'ACTIVE') AS user_status, u.manager_id,
                        m.name AS manager_name, a.session_status, a.session_updated, a.trading_mode,
                        1 AS automation_enabled, COALESCE(w.status, '') AS worker_status,
                        COALESCE(w.last_heartbeat_at, '') AS worker_heartbeat, COALESCE(w.broker_connected, 0) AS broker_connected,
                        COALESCE(w.open_orders, 0) AS open_orders, '' AS desired_state, a.in_roster
                   FROM accounts a LEFT JOIN users u ON u.id = a.user_id LEFT JOIN users m ON m.id = u.manager_id
                   LEFT JOIN workers w ON w.account_id = a.account_id"""
        params: tuple = ()
        if account_ids is not None:
            if not account_ids:
                return []
            w, params = _in("a.account_id", account_ids)
            sql += f" WHERE {w}"
        out = self._rows(sql + " ORDER BY client_name, a.account_id", params)
        for a in out:
            a["masked_account"] = mask_account(a["account_id"])
            if a["user_id"] is None:
                a["user_id"] = 0
        return out

    def account(self, account_id: str) -> dict | None:
        r = self.accounts([account_id])
        return r[0] if r else None

    def clients(self, account_ids=None) -> list[dict]:
        by: dict = {}
        for a in self.accounts(account_ids):
            c = by.setdefault(a["user_id"], {"client_id": a["user_id"], "name": a["client_name"], "username": a["username"],
                                             "role": a["role"], "status": a["user_status"], "manager_name": a["manager_name"],
                                             "accounts": []})
            c["accounts"].append(a)
        return sorted(by.values(), key=lambda c: (c["name"] or "").lower())

    def account_by_broker_order_id(self, broker: str, broker_order_id: str) -> str | None:
        if not broker_order_id:
            return None
        r = self._one("SELECT account_id FROM orders WHERE broker_order_id = ? AND (broker = '' OR LOWER(broker) = LOWER(?)) "
                      "ORDER BY created_at DESC LIMIT 1", (str(broker_order_id), broker))
        return r["account_id"] if r else None

    def account_for_broker_client(self, broker: str, client_code: str) -> str | None:
        if not client_code:
            return None
        r = self._one("SELECT account_id FROM accounts WHERE account_id = ? COLLATE NOCASE AND (broker = '' OR LOWER(broker) = LOWER(?))",
                      (str(client_code), broker))
        return r["account_id"] if r else None

    # --- orders / fills -------------------------------------------------------------------------------
    def orders(self, account_ids, day=None, status=None, search=None, limit=200) -> list[dict]:
        if not account_ids:
            return []
        w, p = _in("o.account_id", account_ids)
        clauses, params = [w], list(p)
        if day:
            clauses.append("substr(o.created_at, 1, 10) = ?")
            params.append(day)
        if status:
            st = status.upper()
            if st == "PENDING":
                clauses.append(f"o.status IN ({','.join('?' * len(WORKING))})")
                params.extend(WORKING)
            elif st == "FILLED":
                clauses.append("o.status = 'COMPLETE'")
            else:
                clauses.append("o.status = ?")
                params.append(st)
        if search:
            clauses.append("(o.symbol LIKE ? OR o.internal_order_id LIKE ? OR o.broker_order_id LIKE ?)")
            params.extend([f"%{search}%"] * 3)
        return self._rows(f"""
            SELECT o.internal_order_id, o.broker_order_id, o.account_id, o.broker, o.trading_mode, o.strategy_id, o.exchange,
                   o.symbol, o.side, o.product, o.order_type, o.ordered_quantity, o.filled_quantity, o.price, o.average_price,
                   o.status, o.status_message, '' AS trade_id, o.created_at, o.updated_at,
                   COALESCE(NULLIF(a.client_name, ''), u.name, '') AS client_name
              FROM orders o LEFT JOIN accounts a ON a.account_id = o.account_id LEFT JOIN users u ON u.id = a.user_id
             WHERE {' AND '.join(clauses)} ORDER BY o.created_at DESC LIMIT ?""", (*params, int(limit)))

    def order(self, account_ids, ref) -> dict | None:
        if not account_ids or not ref:
            return None
        w, p = _in("o.account_id", account_ids)
        o = self._one(f"""SELECT o.*, COALESCE(NULLIF(a.client_name, ''), u.name, '') AS client_name FROM orders o
                            LEFT JOIN accounts a ON a.account_id = o.account_id LEFT JOIN users u ON u.id = a.user_id
                           WHERE (o.internal_order_id = ? OR o.broker_order_id = ?) AND {w}
                           ORDER BY o.created_at DESC LIMIT 1""", (ref, ref, *p))
        if not o:
            return None
        o["trade_id"] = ""
        o["lifecycle"] = self._rows("""SELECT from_status, to_status, source, filled_quantity, average_price, message, at
                                         FROM order_events WHERE internal_order_id = ? ORDER BY id""", (o["internal_order_id"],))
        o["fills"] = self._rows("""SELECT fill_id, exchange_trade_id, quantity, price, filled_at, 'BROKER_TRADEBOOK' AS source
                                     FROM fills WHERE internal_order_id = ? ORDER BY filled_at, id""", (o["internal_order_id"],))
        return o

    def fills(self, account_ids, day=None, limit=200) -> list[dict]:
        if not account_ids:
            return []
        w, p = _in("f.account_id", account_ids)
        params = list(p)
        extra = ""
        if day:
            extra = " AND substr(f.filled_at, 1, 10) = ?"
            params.append(day)
        return self._rows(f"""
            SELECT f.fill_id, f.exchange_trade_id, f.broker_order_id, f.internal_order_id, f.account_id,
                   COALESCE(a.broker, '') AS broker, f.exchange, f.side, f.quantity, f.price, f.filled_at,
                   'BROKER_TRADEBOOK' AS source, f.symbol, COALESCE(o.trading_mode, a.trading_mode, '') AS trading_mode,
                   COALESCE(NULLIF(a.client_name, ''), u.name, '') AS client_name
              FROM fills f LEFT JOIN orders o ON o.internal_order_id = f.internal_order_id
              LEFT JOIN accounts a ON a.account_id = f.account_id LEFT JOIN users u ON u.id = a.user_id
             WHERE {w}{extra} ORDER BY f.filled_at DESC, f.id DESC LIMIT ?""", (*params, int(limit)))

    def order_counts(self, account_ids, day) -> dict:
        base = {"total": 0, "filled": 0, "partial": 0, "pending": 0, "rejected": 0, "cancelled": 0, "failed": 0}
        out = {"_all": dict(base)}
        if not account_ids:
            return out
        w, p = _in("account_id", account_ids)
        for r in self._rows(f"SELECT account_id, status, COUNT(*) AS n FROM orders WHERE {w} AND substr(created_at,1,10) = ? "
                            "GROUP BY account_id, status", (*p, day)):
            key = {"COMPLETE": "filled", "PARTIALLY_FILLED": "partial", "REJECTED": "rejected", "CANCELLED": "cancelled",
                   "EXPIRED": "cancelled", "FAILED": "failed"}.get(r["status"], "pending")
            for b in (out.setdefault(r["account_id"], dict(base)), out["_all"]):
                b[key] += r["n"]
                b["total"] += r["n"]
                if r["status"] == "PARTIALLY_FILLED":
                    b["pending"] += r["n"]
        return out

    def order_counts_since(self, account_id: str, since: str) -> dict:
        r = self._one("""SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'COMPLETE' THEN 1 ELSE 0 END) AS filled,
                                SUM(CASE WHEN status IN ('REJECTED','FAILED') THEN 1 ELSE 0 END) AS rejected,
                                SUM(CASE WHEN status IN ('CANCELLED','EXPIRED') THEN 1 ELSE 0 END) AS cancelled
                           FROM orders WHERE account_id = ? AND substr(created_at,1,10) >= ?""", (account_id, since)) or {}
        return {k: int(r.get(k) or 0) for k in ("total", "filled", "rejected", "cancelled")}

    # --- book: positions & holdings ----------------------------------------------------------------
    def positions(self, account_ids, state="open", day=None, ltp=None) -> list[dict]:
        if not account_ids:
            return []
        w, p = _in("b.account_id", account_ids)
        where = [w]
        if state == "open":
            where.append("b.net_quantity != 0")
        elif state == "closed":
            where.append("b.net_quantity = 0 AND (b.day_buy_qty > 0 OR b.day_sell_qty > 0 OR b.baseline_qty != 0)")
        rows = self._rows(f"""
            SELECT b.*, COALESCE(a.broker, '') AS broker, COALESCE(a.trading_mode, '') AS trading_mode,
                   COALESCE(NULLIF(a.client_name, ''), u.name, '') AS client_name
              FROM book_positions b LEFT JOIN accounts a ON a.account_id = b.account_id LEFT JOIN users u ON u.id = a.user_id
             WHERE {' AND '.join(where)} ORDER BY client_name, b.account_id, b.symbol""", p)
        for r in rows:
            price = None
            if ltp is not None and r["net_quantity"]:
                try:
                    price = ltp(r)
                except Exception:
                    price = None
            if price is None and r["last_price"]:
                price = float(r["last_price"])
            u = bk.unrealized(r, price)
            realized = bk.dec(r["realized"])
            net = int(r["net_quantity"])
            r.update({
                "trade_leg_id": f"{r['account_id']}:{r['exchange']}:{r['exchange_token']}:{r['product']}",
                "direction": "LONG" if net > 0 else "SHORT" if net < 0 else "FLAT",
                "open_quantity": abs(net), "closed_quantity": min(int(r["day_buy_qty"]), int(r["day_sell_qty"])),
                # None (shown "–") when the broker reported no cost: never present 0 as an average price
                "average_entry_price": float(bk.dec(r["average_price"])) if (r["cost_known"] or not net) else None,
                "average_exit_price": None,
                "pnl_source": "broker" if (net and not r["cost_known"] and u is not None) else ("unpriced" if u is None else "book"),
                "ltp": price, "realized_pnl_d": realized, "unrealized_pnl_d": u,
                "realized_pnl": float(realized), "unrealized_pnl": None if u is None else float(u),
                "status": "OPEN" if net else "CLOSED", "opened_at": r["baseline_at"], "closed_at": "",
            })
        return rows

    def holdings(self, account_ids, ltp=None) -> list[dict]:
        if not account_ids:
            return []
        w, p = _in("h.account_id", account_ids)
        rows = self._rows(f"""
            SELECT h.*, COALESCE(a.broker, '') AS broker, COALESCE(NULLIF(a.client_name, ''), u.name, '') AS client_name
              FROM book_holdings h LEFT JOIN accounts a ON a.account_id = h.account_id LEFT JOIN users u ON u.id = a.user_id
             WHERE {w} AND (h.quantity != 0 OR h.baseline_qty != 0) ORDER BY client_name, h.account_id, h.symbol""", p)
        for r in rows:
            price = None
            if ltp is not None:
                try:
                    price = ltp({**r, "instrument_class": "EQ"})
                except Exception:
                    price = None
            price = price or (float(r["last_price"]) or None)
            avg = bk.dec(r["average_price"])
            r["ltp"] = price
            r["average_price"] = float(avg)
            r["value"] = float(bk.q2(bk.dec(price) * r["quantity"])) if price else None
            r["pnl"] = float(bk.q2(r["quantity"] * (bk.dec(price) - avg))) if price and avg else None
            r["change"] = r["quantity"] - r["baseline_qty"]
        return rows

    def baselines(self, account_ids) -> dict:
        if not account_ids:
            return {}
        w, p = _in("account_id", account_ids)
        out: dict = {}
        for r in self._rows(f"SELECT * FROM book_baselines WHERE {w}", p):
            out.setdefault(r["account_id"], {})[r["kind"]] = r
        return out

    def pnl(self, account_ids, since_day=None, ltp=None) -> dict:
        per: dict = {}
        total = _bucket()
        for pos in self.positions(account_ids, "all", ltp=ltp):
            b = per.setdefault(pos["account_id"], {**_bucket(), "client_name": pos["client_name"], "broker": pos["broker"]})
            for x in (b, total):
                x["realized"] += pos["realized_pnl_d"]
                if pos["unrealized_pnl_d"] is not None:
                    x["unrealized"] += pos["unrealized_pnl_d"]
                elif pos["net_quantity"]:
                    x["unpriced_open_legs"] += 1
                if pos["net_quantity"]:
                    x["open_legs"] += 1
                else:
                    x["closed_legs"] += 1
                    if pos["realized_pnl_d"] > 0:
                        x["wins"] += 1
                    elif pos["realized_pnl_d"] < 0:
                        x["losses"] += 1
        for b in [*per.values(), total]:
            _finish(b)
        return {"accounts": per, "total": total}

    def broker_positions(self, account_ids) -> list[dict]:
        """The broker's own position book as last fetched (snapshot), not the derived book."""
        import json
        out = []
        for a in self.accounts(account_ids):
            s = self._one("SELECT * FROM snapshots WHERE account_id = ? AND kind = 'positions' AND ok = 1 ORDER BY id DESC LIMIT 1",
                          (a["account_id"],))
            if not s:
                continue
            for r in json.loads(s["payload"] or "[]"):
                out.append({"account_id": a["account_id"], "trading_mode": a["trading_mode"], "exchange": r.get("exchange"),
                            "symbol": r.get("broker_symbol"), "product": r.get("product"), "net_quantity": r.get("net_quantity"),
                            "average_price": r.get("average_price"), "last_price": r.get("last_price"), "pnl": r.get("pnl"),
                            "reported_at": s["at"], "source": s["source"], "client_name": a["client_name"]})
        return out

    def alerts(self, account_ids, limit=50, unacknowledged=False) -> list[dict]:
        if not account_ids:
            return []
        import json
        w, p = _in("account_id", account_ids)
        rows = self._rows(f"""SELECT id, type, account_id, symbol, side, status, message, trading_mode, payload, occurred_at
                                FROM events WHERE {w} AND type IN ('ACCOUNT_ALERT','RECONCILE_MISMATCH')
                               ORDER BY id DESC LIMIT ?""", (*p, int(limit)))
        out = []
        for r in rows:
            pl = json.loads(r["payload"] or "{}")
            out.append({"id": r["id"], "kind": pl.get("kind") or r["type"], "severity": pl.get("severity") or "WARNING",
                        "trading_mode": r["trading_mode"], "account_id": r["account_id"], "symbol": r["symbol"], "side": r["side"],
                        "order_status": r["status"], "message": r["message"], "created_at": r["occurred_at"], "acknowledged_at": None})
        return out

    def workers(self, account_ids=None) -> list[dict]:
        rows = self._rows("SELECT account_id, worker_id, status, broker, broker_connected, last_heartbeat_at, open_orders, last_error "
                          "FROM workers ORDER BY account_id")
        return [r for r in rows if account_ids is None or r["account_id"] in account_ids]


def _bucket() -> dict:
    return {"realized": D0, "unrealized": D0, "open_legs": 0, "closed_legs": 0, "wins": 0, "losses": 0, "unpriced_open_legs": 0}


def _finish(b: dict) -> None:
    b["net"] = b["realized"] + b["unrealized"]
    decided = b["wins"] + b["losses"]
    b["win_rate"] = round(100.0 * b["wins"] / decided, 1) if decided else None
    for k in ("realized", "unrealized", "net"):
        b[k] = float(b[k])


def today() -> str:
    return today_ist()
