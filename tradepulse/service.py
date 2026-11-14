"""Scoped data access shared by the dashboard API, and the Telegram bot.

Every method takes ``scope`` - the account ids the caller may see - and never returns anything
outside it. The scope is computed once per request/message (``Scope``) from the OMS user's role
(same rules as the OMS) or from a Telegram group's configured accounts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import IST, now_ist, today_ist
from .localdata import mask_account

SEGMENTS = {"CE": "OPT", "PE": "OPT", "FUT": "FUT", "EQ": "EQ"}


@dataclass
class Scope:
    account_ids: list[str]
    label: str = ""                      # "Mathew (CLIENT)" / "group: VIP Clients"
    user: dict | None = None
    group_ids: list[int] = field(default_factory=list)
    modes: list[str] | None = None       # trading modes this chat sees; None = every mode (private chats)
    name: str = ""                       # the group's own name, for group chats

    def allows(self, account_id: str) -> bool:
        return account_id in self.account_ids

    @property
    def is_group(self) -> bool:
        return bool(self.group_ids)

    def narrow(self, account_ids: list[str]) -> "Scope":
        """The same chat looking at fewer accounts (keeps the trading-mode filter)."""
        return Scope([a for a in account_ids if self.allows(a)], self.label, self.user, list(self.group_ids),
                     self.modes, self.name)

    def mode_ok(self, mode: str | None) -> bool:
        return self.modes is None or not mode or str(mode).upper() in self.modes


class DataService:
    def __init__(self, oms, store, bus=None):
        self.oms, self.store, self.bus = oms, store, bus

    # --- scopes ------------------------------------------------------------------------
    def scope_for_user(self, user: dict) -> Scope:
        ids = self.oms.visible_account_ids(user)
        return Scope(ids, f"{user.get('name')} ({user.get('role')})", user=user)

    def scope_for_groups(self, groups: list[dict], user: dict | None = None) -> Scope:
        ids = sorted({a for g in groups if g["enabled"] for a in g["account_ids"]})
        global_modes = [str(m).upper() for m in (self.store.setting("notify_modes") or [])]
        modes = sorted({m for g in groups for m in (g.get("modes") or global_modes)})
        names = ", ".join(g["name"] for g in groups)
        return Scope(ids, "group: " + names, user=user, group_ids=[g["id"] for g in groups], modes=modes, name=names)

    def book_ids(self, scope: Scope, ids: list[str] | None = None) -> list[str]:
        """Accounts whose position book this chat may see: a paper-broker account's book is PAPER, every
        other account's book is the real broker book (LIVE)."""
        ids = [i for i in (ids if ids is not None else scope.account_ids) if scope.allows(i)]
        if scope.modes is None:
            return ids
        brokers = {a["account_id"]: str(a.get("broker") or "").lower() for a in self.oms.accounts(ids)}
        return [i for i in ids if scope.mode_ok("PAPER" if brokers.get(i) == "paper" else "LIVE")]

    # --- live price (read-only; the OMS market-data publisher owns LTP:*) ----------------
    def ltp(self, leg: dict) -> float | None:
        if self.bus is None or not hasattr(self.bus, "hget_any"):
            return None
        seg = SEGMENTS.get(str(leg.get("instrument_class") or "").upper())
        if not seg:
            return None
        try:
            raw = self.bus.hget_any(f"LTP:{leg['exchange']}:{seg}", str(leg["exchange_token"]))
        except Exception:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            price = float(data.get("ltp") if isinstance(data, dict) else data)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    # --- accounts ----------------------------------------------------------------------
    def accounts(self, scope: Scope) -> list[dict]:
        mon = self.store.monitored()
        groups = self.store.groups()
        out = []
        for a in self.oms.accounts(scope.account_ids):
            m = mon.get(a["account_id"], {})
            a["monitored"] = bool(m.get("enabled"))
            a["muted"] = bool(m.get("muted"))
            a["groups"] = [{"id": g["id"], "name": g["name"], "color": g["color"]}
                           for g in groups if a["account_id"] in g["account_ids"]]
            out.append(a)
        return out

    def resolve_account(self, scope: Scope, ref: str) -> list[str]:
        """'Mathew', 'J252505', '2505', 'bharti' -> account ids within scope (possibly several)."""
        ref = str(ref or "").strip().lower()
        if not ref or ref in ("all", "*", "every", "everything"):
            return list(scope.account_ids)
        accts = self.oms.accounts(scope.account_ids)
        exact = [a["account_id"] for a in accts if a["account_id"].lower() == ref]
        if exact:
            return exact
        hits = [a["account_id"] for a in accts if ref in (a["client_name"] or "").lower()
                or a["account_id"].lower().endswith(ref.lstrip("*"))
                or (a["username"] or "").lower() == ref]
        if hits:
            return hits
        words = [w for w in re.split(r"\W+", ref) if len(w) > 2]
        return [a["account_id"] for a in accts if any(w in (a["client_name"] or "").lower() for w in words)]

    # --- overview ----------------------------------------------------------------------
    def overview(self, scope: Scope, day: str | None = None) -> dict:
        day = day or today_ist()
        ids = scope.account_ids
        accts = self.accounts(scope)
        monitored = [a for a in accts if a["monitored"]]
        counts = self.oms.order_counts(ids, day)
        pnl = self.oms.pnl(ids, since_day=day, ltp=self.ltp)
        fills = self.oms.fills(ids, day, limit=10000)
        open_pos = self.oms.positions(ids, "open", ltp=self.ltp)
        ev = self.store.one(f"""SELECT COUNT(*) AS n, MAX(received_at) AS last FROM events
                                WHERE substr(occurred_at,1,10) = ? AND account_id IN ({','.join('?' * len(ids)) or "''"})""",
                            (day, *ids)) if ids else {"n": 0, "last": None}
        last_webhook = self.store.one("SELECT broker, at, outcome FROM webhook_log ORDER BY id DESC LIMIT 1")
        notif = {r["status"]: r["n"] for r in self.store.rows(
            "SELECT status, COUNT(*) AS n FROM notifications WHERE substr(created_at,1,10) = ? GROUP BY status", (day,))}
        return {
            "day": day,
            "clients": len({a["user_id"] for a in accts}),
            "accounts": len(accts),
            "monitored_accounts": len(monitored),
            "monitored_clients": len({a["user_id"] for a in monitored}),
            "orders": counts["_all"],
            "orders_by_account": {k: v for k, v in counts.items() if k != "_all"},
            "trades_today": len(fills),
            "open_positions": len(open_pos),
            "pnl": pnl,
            "events_today": (ev or {}).get("n") or 0,
            "last_event_at": (ev or {}).get("last"),
            "last_webhook": last_webhook,
            "notifications_today": notif,
        }

    def account_detail(self, scope: Scope, account_id: str, day: str | None = None) -> dict | None:
        if not scope.allows(account_id):
            return None
        day = day or today_ist()
        a = next((x for x in self.accounts(scope) if x["account_id"] == account_id), None)
        if not a:
            return None
        one = [account_id]
        return {
            "account": a,
            "orders_today": self.oms.order_counts(one, day).get(account_id, self.oms.order_counts(one, day)["_all"]),
            "pnl": self.oms.pnl(one, since_day=day, ltp=self.ltp)["total"],
            "positions": self.oms.positions(one, "open", ltp=self.ltp),
            "orders": self.oms.orders(one, limit=50),
            "fills": self.oms.fills(one, limit=50),
            "alerts": self.oms.alerts(one, limit=20),
            "events": self.events(scope, account_id=account_id, limit=50),
            "workers": self.oms.workers(one),
            "groups": a["groups"],
            "webhook_health": self.store.rows("SELECT broker, at, outcome, detail FROM webhook_log WHERE account_id = ? "
                                              "ORDER BY id DESC LIMIT 10", (account_id,)),
        }

    # --- event store (TradePulse's own) --------------------------------------------------
    def events(self, scope: Scope, account_id: str | None = None, types: list[str] | None = None,
               broker: str | None = None, day: str | None = None, date_from: str | None = None,
               date_to: str | None = None, search: str | None = None, client: str | None = None,
               before_id: int | None = None, limit: int = 200) -> list[dict]:
        ids = [account_id] if account_id else scope.account_ids
        ids = [i for i in ids if scope.allows(i)]
        if not ids:
            return []
        where = [f"account_id IN ({','.join('?' * len(ids))})"]
        p: list = list(ids)
        if types:
            where.append(f"type IN ({','.join('?' * len(types))})")
            p.extend(types)
        if broker:
            where.append("broker = ?")
            p.append(broker.lower())
        if client:
            where.append("client_name LIKE ?")
            p.append(f"%{client}%")
        if day:
            where.append("substr(occurred_at,1,10) = ?")
            p.append(day)
        if date_from:
            where.append("substr(occurred_at,1,10) >= ?")
            p.append(date_from)
        if date_to:
            where.append("substr(occurred_at,1,10) <= ?")
            p.append(date_to)
        if search:
            where.append("(symbol LIKE ? OR order_id LIKE ? OR internal_order_id LIKE ? OR trade_id LIKE ? OR client_name LIKE ?)")
            p.extend([f"%{search}%"] * 5)
        if scope.modes is not None:
            ms = scope.modes or ["-"]
            where.append(f"(COALESCE(trading_mode, '') = '' OR trading_mode IN ({','.join('?' * len(ms))}))")
            p.extend(ms)
        if before_id:
            where.append("id < ?")
            p.append(int(before_id))
        rows = self.store.rows(f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                               (*p, min(int(limit), 5000)))
        for r in rows:
            r["payload"] = json.loads(r["payload"] or "{}")
            r["masked_account"] = mask_account(r["account_id"])
        return rows

    def event(self, scope: Scope, event_id: str) -> dict | None:
        r = self.store.one("SELECT * FROM events WHERE event_id = ?", (event_id,))
        if not r or not scope.allows(r["account_id"]):
            return None
        r["payload"] = json.loads(r["payload"] or "{}")
        r["notifications"] = self.store.rows("""SELECT n.id, n.status, n.attempts, n.last_error, n.sent_at, c.title, c.kind
                                                  FROM notifications n JOIN channels c ON c.id = n.channel_id
                                                 WHERE n.event_id = ?""", (event_id,))
        return r

    def event_stats(self, scope: Scope, day: str | None = None) -> dict:
        day = day or today_ist()
        ids = scope.account_ids
        if not ids:
            return {"by_type": {}, "by_hour": {}, "by_account": {}}
        marks = ",".join("?" * len(ids))
        by_type = {r["type"]: r["n"] for r in self.store.rows(
            f"SELECT type, COUNT(*) AS n FROM events WHERE substr(occurred_at,1,10) = ? AND account_id IN ({marks}) GROUP BY type",
            (day, *ids))}
        by_hour = {r["h"]: r["n"] for r in self.store.rows(
            f"SELECT substr(occurred_at,12,2) AS h, COUNT(*) AS n FROM events WHERE substr(occurred_at,1,10) = ? "
            f"AND account_id IN ({marks}) GROUP BY h", (day, *ids))}
        by_account = {r["account_id"]: r["n"] for r in self.store.rows(
            f"SELECT account_id, COUNT(*) AS n FROM events WHERE substr(occurred_at,1,10) = ? AND account_id IN ({marks}) "
            f"GROUP BY account_id", (day, *ids))}
        return {"day": day, "by_type": by_type, "by_hour": by_hour, "by_account": by_account}

    # --- performance -------------------------------------------------------------------------
    def performance(self, scope: Scope, period: str = "today") -> dict:
        start = period_start(period)
        pnl = self.oms.pnl(self.book_ids(scope), since_day=start, ltp=self.ltp)
        names = {a["account_id"]: a for a in self.oms.accounts(scope.account_ids)}
        rows = []
        for aid in scope.account_ids:
            b = pnl["accounts"].get(aid)
            a = names.get(aid, {})
            counts = self._order_counts_range(aid, start)
            rows.append({"account_id": aid, "masked_account": mask_account(aid), "client_name": a.get("client_name", ""),
                         "broker": a.get("broker", ""), **(b or {"realized": 0.0, "unrealized": 0.0, "net": 0.0,
                                                                 "open_legs": 0, "closed_legs": 0, "wins": 0,
                                                                 "losses": 0, "win_rate": None}),
                         "orders": counts})
        return {"period": period, "since": start, "total": pnl["total"], "accounts": rows}

    def _order_counts_range(self, account_id: str, since: str) -> dict:
        d = self.oms.order_counts_since(account_id, since)
        d["fill_rate"] = round(100.0 * d["filled"] / d["total"], 1) if d["total"] else None
        d["reject_rate"] = round(100.0 * d["rejected"] / d["total"], 1) if d["total"] else None
        return d

    # --- holdings & reconciliation -------------------------------------------------------------------
    def holdings(self, scope: Scope, account_id: str | None = None) -> list[dict]:
        ids = [account_id] if account_id else scope.account_ids
        ids = [i for i in ids if scope.allows(i)]
        rows = self.oms.holdings(ids, ltp=self.ltp)
        for r in rows:
            r["masked_account"] = mask_account(r["account_id"])
        return rows

    def reconcile_status(self, scope: Scope) -> dict:
        ids = scope.account_ids
        base = self.oms.baselines(ids)
        accts = self.oms.accounts(ids)
        mon = self.store.monitored_ids()
        rows = []
        for a in accts:
            b = base.get(a["account_id"], {})
            last_fail = self.store.one("SELECT kind, error, at FROM snapshots WHERE account_id = ? AND ok = 0 ORDER BY id DESC LIMIT 1",
                                       (a["account_id"],))
            open_diffs = self.store.one("""SELECT COUNT(*) AS n FROM reconcile_diffs WHERE account_id = ? AND at >=
                                            COALESCE((SELECT MAX(at) FROM book_baselines WHERE account_id = ?), '')""",
                                        (a["account_id"], a["account_id"]))["n"]
            rows.append({"account_id": a["account_id"], "masked_account": a["masked_account"], "client_name": a["client_name"],
                         "broker": a["broker"], "monitored": a["account_id"] in mon, "session_status": a["session_status"],
                         "positions_at": (b.get("positions") or {}).get("at", ""),
                         "positions_source": (b.get("positions") or {}).get("source", ""),
                         "holdings_at": (b.get("holdings") or {}).get("at", ""),
                         "holdings_source": (b.get("holdings") or {}).get("source", ""),
                         "last_failure": last_fail, "mismatches": open_diffs})
        if ids:
            marks = ",".join("?" * len(ids))
            runs = self.store.rows(f"""SELECT * FROM reconcile_runs WHERE id IN (SELECT DISTINCT run_id FROM snapshots
                                         WHERE account_id IN ({marks})) OR trigger LIKE 'SCHEDULED%' ORDER BY id DESC LIMIT 30""",
                                   tuple(ids))
            diffs = self.store.rows(f"""SELECT d.*, r.trigger FROM reconcile_diffs d LEFT JOIN reconcile_runs r ON r.id = d.run_id
                                          WHERE d.account_id IN ({marks}) ORDER BY d.id DESC LIMIT 200""", tuple(ids))
        else:
            runs, diffs = [], []
        for d in diffs:
            d["masked_account"] = mask_account(d["account_id"])
        return {"accounts": rows, "runs": runs, "diffs": diffs}


def period_start(period: str) -> str:
    n = now_ist()
    if period == "week":
        return (n - timedelta(days=n.weekday())).strftime("%Y-%m-%d")
    if period == "month":
        return n.strftime("%Y-%m-01")
    if period == "all":
        return "2000-01-01"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(period)):
        return period
    return n.strftime("%Y-%m-%d")


def parse_iso(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s))
        return d if d.tzinfo else d.replace(tzinfo=IST)
    except (TypeError, ValueError):
        return None
