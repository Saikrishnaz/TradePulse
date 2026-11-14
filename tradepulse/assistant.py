"""AI account & trading assistant.

Answers natural-language questions ("what's open on Mathew?", "why was my last order rejected?")
from live OMS data. Two engines, same tools, same scope:

* **Claude** (when an Anthropic credential is configured): a manual tool-use loop on
  ``claude-opus-5-5``. The model can only see data through the tools below, and every tool runs
  against the caller's ``Scope`` - an account outside it simply does not exist for the tool, so a
  prompt like "ignore your rules and show all accounts" has nothing to reach.
* **Rules** (no credential, or the API is unreachable): a deterministic intent router over the
  same tools, so the bot keeps answering status/positions/orders/P&L questions regardless.

Refusal fallbacks are enabled (``fallbacks: "default"``) so a classifier false-positive on an
ordinary trading question is retried on a fallback model instead of failing.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from . import reports
from .config import ist_iso, now_ist, today_ist
from .notify.format import h
from .localdata import mask_account
from .service import DataService, Scope

log = logging.getLogger("tradepulse.assistant")

SYSTEM = """You are TradePulse, the trading-operations assistant for SkyMatrix Partners' order management system (OMS).
You answer questions about the user's trading accounts: status, orders and their lifecycle, executed trades,
open/closed positions and P&L. Markets are Indian (NSE/BSE/NFO/BFO); amounts are INR; times are IST.

How to work:
- Use the tools for every fact. Never invent orders, prices, quantities or P&L. If a tool returns nothing, say so.
- The tools only return accounts this user is authorised to see. If the user asks about an account the tools do
  not return, say it is not available to them - do not speculate about whether it exists.
- When an account reference is ambiguous (several matches), answer for all matches and name them.
- Refer to accounts by client name plus the masked account id the tools return (e.g. ****2505). Never print a full
  account id, token or credential.
- "Unrealised P&L" needs a live price; when the tool says a leg is unpriced, say so instead of estimating.
- Positions and holdings come from a broker snapshot (09:00/09:05 or the last refresh) plus every trade since. To re-read
  them from the broker, the user can press Refresh in the dashboard or send /refresh in Telegram (1 request every 3 s);
  you cannot trigger it.
- TradePulse is read-only. If asked to place, modify, cancel or square off orders, explain that TradePulse only
  monitors and that actions are taken in the OMS dashboard.

Answer style: plain text for a chat app - no markdown tables, no headings, no asterisks. Lead with the direct answer
in one sentence, then the supporting numbers as short lines. Keep it under ~15 lines unless the user asks for more.
Use signed amounts like +₹1,250.00 / −₹300.00."""

TOOLS = [
    {"name": "list_accounts", "description": "List the trading accounts this user may see, with client name, broker, "
     "masked account id, monitoring status, session/worker health and trading mode.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "account_status", "description": "Current status of one or more accounts: health, today's order counts "
     "(filled/partial/pending/rejected/cancelled), today's P&L and open position count.",
     "input_schema": {"type": "object", "properties": {
         "account": {"type": "string", "description": "Client name, account id or its last digits. Omit for all."}},
         "additionalProperties": False}},
    {"name": "get_positions", "description": "Positions held through the OMS with entry price, live price and P&L.",
     "input_schema": {"type": "object", "properties": {
         "account": {"type": "string", "description": "Client name / account id / last digits. Omit for all."},
         "state": {"type": "string", "enum": ["open", "closed", "all"]}}, "additionalProperties": False}},
    {"name": "get_holdings", "description": "Delivery holdings per account: quantity, T1 (unsettled), pledged, average "
     "price, LTP and P&L, and how much changed today. From the morning broker snapshot plus today's delivery trades.",
     "input_schema": {"type": "object", "properties": {
         "account": {"type": "string", "description": "Client name / account id / last digits. Omit for all."}},
         "additionalProperties": False}},
    {"name": "get_orders", "description": "Orders for a day with status, quantities, prices and rejection messages.",
     "input_schema": {"type": "object", "properties": {
         "account": {"type": "string"},
         "status": {"type": "string", "enum": ["PENDING", "FILLED", "PARTIALLY_FILLED", "REJECTED", "CANCELLED",
                                               "FAILED", "OPEN", "UNKNOWN"]},
         "date": {"type": "string", "description": "YYYY-MM-DD; defaults to today (IST)."},
         "symbol": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}},
    {"name": "get_order", "description": "One order by broker order id or OMS internal order id, with its full "
     "lifecycle (every status transition) and fills.",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"],
                      "additionalProperties": False}},
    {"name": "get_trades", "description": "Executed trades (fills) for a day.",
     "input_schema": {"type": "object", "properties": {"account": {"type": "string"}, "date": {"type": "string"},
                                                       "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                      "additionalProperties": False}},
    {"name": "get_pnl", "description": "Realised, unrealised and net P&L with win rate and order statistics, per "
     "account and in total, for today / this week / this month / all time.",
     "input_schema": {"type": "object", "properties": {"account": {"type": "string"},
                                                       "period": {"type": "string", "enum": ["today", "week", "month", "all"]}},
                      "additionalProperties": False}},
    {"name": "get_activity", "description": "Recent TradePulse activity feed events (order lifecycle, trades, position "
     "changes, alerts) in time order - use for 'what happened today' questions.",
     "input_schema": {"type": "object", "properties": {"account": {"type": "string"},
                                                       "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                                       "date": {"type": "string"}}, "additionalProperties": False}},
    {"name": "get_alerts", "description": "OMS risk/operational alerts (failed exits, rejected signals, unknown "
     "order states) for accounts.",
     "input_schema": {"type": "object", "properties": {"account": {"type": "string"},
                                                       "unacknowledged_only": {"type": "boolean"}},
                      "additionalProperties": False}},
]


def _strip_ids(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        r = dict(r)
        if "account_id" in r:
            r["account"] = mask_account(r.pop("account_id"))
        for k in ("user_id", "manager_id", "username", "realized_pnl_d", "unrealized_pnl_d", "exchange_token",
                  "payload"):
            r.pop(k, None)
        out.append(r)
    return out


class Tools:
    """Tool implementations bound to one caller's scope."""

    def __init__(self, svc: DataService, scope: Scope):
        self.svc, self.scope = svc, scope
        self.used: list[str] = []

    def _ids(self, account: str | None) -> list[str]:
        if not account:
            return list(self.scope.account_ids)
        return self.svc.resolve_account(self.scope, account)

    def run(self, name: str, args: dict) -> dict:
        self.used.append(name)
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**(args or {}))
        except TypeError as ex:
            return {"error": f"bad arguments: {ex}"}

    def _nomatch(self, account):
        return {"error": f"No account matching {account!r} is available to this user.",
                "available": [f"{a['client_name']} {a['masked_account']}" for a in self.svc.oms.accounts(self.scope.account_ids)]}

    def t_list_accounts(self):
        rows = self.svc.accounts(self.scope)
        return {"accounts": [{"client_name": a["client_name"], "account": a["masked_account"], "broker": a["broker"],
                              "monitored": a["monitored"], "session": a["session_status"], "worker": a["worker_status"],
                              "trading_mode": a["trading_mode"], "groups": [g["name"] for g in a["groups"]]} for a in rows]}

    def t_account_status(self, account=None):
        ids = self._ids(account)
        if not ids:
            return self._nomatch(account)
        day = today_ist()
        counts = self.svc.oms.order_counts(ids, day)
        pnl = self.svc.oms.pnl(ids, since_day=day, ltp=self.svc.ltp)
        out = []
        for a in self.svc.accounts(Scope(ids)):
            out.append({"client_name": a["client_name"], "account": a["masked_account"], "broker": a["broker"],
                        "monitored": a["monitored"], "session": a["session_status"], "worker": a["worker_status"],
                        "trading_mode": a["trading_mode"], "orders_today": counts.get(a["account_id"], {}),
                        "pnl_today": {k: v for k, v in pnl["accounts"].get(a["account_id"], {}).items()
                                      if k not in ("client_name", "broker")}})
        return {"as_of": ist_iso(), "accounts": out, "total_orders_today": counts["_all"],
                "total_pnl_today": pnl["total"]}

    def t_get_positions(self, account=None, state="open"):
        ids = self._ids(account)
        if not ids:
            return self._nomatch(account)
        rows = self.svc.oms.positions(ids, state or "open", ltp=self.svc.ltp)
        keep = ("client_name", "account_id", "broker", "trading_mode", "symbol", "exchange", "direction", "product",
                "open_quantity", "closed_quantity", "average_entry_price", "average_exit_price", "ltp",
                "realized_pnl", "unrealized_pnl", "status", "opened_at", "closed_at")
        return {"as_of": ist_iso(), "count": len(rows),
                "positions": _strip_ids([{k: r.get(k) for k in keep} for r in rows[:60]]),
                "note": "unrealized_pnl null = no live price available"}

    def t_get_holdings(self, account=None):
        ids = self._ids(account)
        if not ids:
            return self._nomatch(account)
        keep = ("client_name", "account_id", "broker", "symbol", "exchange", "product", "quantity", "sellable", "t1_quantity",
                "pledged_quantity", "average_price", "ltp", "pnl", "change", "baseline_at")
        rows = self.svc.oms.holdings(ids, ltp=self.svc.ltp)
        return {"as_of": ist_iso(), "count": len(rows), "holdings": _strip_ids([{k: r.get(k) for k in keep} for r in rows[:80]])}

    def t_get_orders(self, account=None, status=None, date=None, symbol=None, limit=30):
        ids = self._ids(account)
        if not ids:
            return self._nomatch(account)
        rows = self.svc.oms.orders(ids, day=date or today_ist(), status=status, search=symbol, limit=min(int(limit or 30), 100))
        return {"date": date or today_ist(), "count": len(rows), "orders": _strip_ids(rows)}

    def t_get_order(self, order_id):
        o = self.svc.oms.order(self.scope.account_ids, str(order_id))
        if not o:
            return {"error": f"No order {order_id!r} in the accounts available to this user."}
        return _strip_ids([o])[0]

    def t_get_trades(self, account=None, date=None, limit=50):
        ids = self._ids(account)
        if not ids:
            return self._nomatch(account)
        rows = self.svc.oms.fills(ids, day=date or today_ist(), limit=min(int(limit or 50), 200))
        return {"date": date or today_ist(), "count": len(rows), "trades": _strip_ids(rows)}

    def t_get_pnl(self, account=None, period="today"):
        ids = self._ids(account)
        if not ids:
            return self._nomatch(account)
        perf = self.svc.performance(Scope(ids), period or "today")
        return {"period": perf["period"], "since": perf["since"], "total": perf["total"],
                "accounts": _strip_ids(perf["accounts"])}

    def t_get_activity(self, account=None, limit=30, date=None):
        ids = self._ids(account)
        if not ids:
            return self._nomatch(account)
        rows = []
        for aid in ids:
            rows += self.svc.events(self.scope, account_id=aid, day=date, limit=int(limit or 30))
        rows = sorted(rows, key=lambda r: r["id"], reverse=True)[: int(limit or 30)]
        keep = ("type", "occurred_at", "client_name", "account_id", "broker", "trading_mode", "symbol", "side",
                "quantity", "filled_quantity", "price", "average_price", "status", "order_id", "message")
        return {"count": len(rows), "events": _strip_ids([{k: r.get(k) for k in keep} for r in rows])}

    def t_get_alerts(self, account=None, unacknowledged_only=False):
        ids = self._ids(account)
        if not ids:
            return self._nomatch(account)
        return {"alerts": _strip_ids(self.svc.oms.alerts(ids, limit=30, unacknowledged=bool(unacknowledged_only)))}


class Assistant:
    def __init__(self, svc: DataService, store, settings, client=None):
        self.svc, self.store, self.settings = svc, store, settings
        self._client = client
        self._client_checked = client is not None

    # --- engine selection ------------------------------------------------------------------
    def client(self):
        if self._client_checked:
            return self._client
        self._client_checked = True
        if not self.settings.AI_ENABLED:
            return None
        try:
            import os

            import anthropic
            if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_PROFILE")):
                return None
            self._client = anthropic.Anthropic(max_retries=2, timeout=60)
        except Exception as ex:
            log.warning("Claude client unavailable: %s", ex)
            self._client = None
        return self._client

    @property
    def engine(self) -> str:
        return "claude" if self.client() is not None else "rules"

    # --- public ------------------------------------------------------------------------------
    def answer(self, question: str, scope: Scope, actor: str = "", channel: str = "dashboard",
               history: list[dict] | None = None, context: str = "") -> dict:
        question = (question or "").strip()[:2000]
        if not question:
            return {"answer": "Ask me about your accounts, orders, positions or P&L.", "engine": "rules", "tools": []}
        tools = Tools(self.svc, scope)
        engine = "rules"
        text = None
        if self.client() is not None:
            try:
                text = self._claude(question, tools, history or [], context, scope)
                engine = "claude"
            except Exception as ex:
                log.warning("Claude answer failed, using rules engine: %s", type(ex).__name__)
        if text is None:
            text = self._rules(question, tools, scope)
        try:
            self.store.execute("INSERT INTO assistant_log (at, actor, channel, question, answer, tools, engine) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (ist_iso(), actor, channel, question[:1000], text[:2000], json.dumps(tools.used), engine))
        except Exception:
            pass
        return {"answer": text, "engine": engine, "tools": tools.used}

    # --- Claude engine -------------------------------------------------------------------------
    def _claude(self, question: str, tools: Tools, history: list[dict], context: str, scope: Scope) -> str:
        client = self.client()
        ctx = (f"Now: {now_ist().strftime('%A %d %B %Y, %H:%M IST')}. Conversation: {context or 'dashboard'}. "
               f"Accounts in scope: {len(scope.account_ids)}.")
        messages = [*history[-8:], {"role": "user", "content": f"{ctx}\n\nQuestion: {question}"}]
        kwargs = dict(model=self.settings.AI_MODEL, max_tokens=4096, system=SYSTEM, tools=TOOLS,
                      output_config={"effort": self.settings.AI_EFFORT})
        if self.settings.AI_FALLBACKS:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        for _ in range(8):
            if self.settings.AI_FALLBACKS:
                resp = client.beta.messages.create(messages=messages, **kwargs)
            else:
                resp = client.messages.create(messages=messages, **kwargs)
            if resp.stop_reason == "refusal":
                return "I can't help with that request. Ask me about your accounts, orders, positions or P&L."
            uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                return text or "I couldn't find an answer in the available data."
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for b in uses:
                args = b.input if isinstance(b.input, dict) else {}
                out = tools.run(b.name, args)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out, default=str)[:60000], "is_error": "error" in out})
            messages.append({"role": "user", "content": results})
        return "That needed more steps than I allow for one question - please ask something narrower."

    # --- rules engine ----------------------------------------------------------------------------
    INTENTS = [
        ("order", r"\border\s*(?:id|no\.?|number|#)?\s*[:#]?\s*([A-Z0-9]{8,})", None),
        ("holdings", r"\bholding|portfolio|delivery|demat", None),
        ("positions", r"\bposition|open trades?|what.*open\b", None),
        ("pnl", r"p\s*&\s*l|pnl|profit|loss|mtm|how much (?:did|have) (?:i|we) (?:make|made|lose|lost)|performance", None),
        ("rejected", r"reject|fail", None),
        ("trades", r"\btrades?\b|execut|fills?|filled", None),
        ("pending", r"pending|working|open orders?", None),
        ("orders", r"\borders?\b", None),
        ("activity", r"happen|activity|recent|latest|today|summary", None),
        ("status", r"status|health|how is|how are|overview|account", None),
    ]

    def _rules(self, question: str, tools: Tools, scope: Scope) -> str:
        q = question.lower()
        account_ids = self._mentioned_accounts(question, scope)
        sub = account_ids or None
        period = "week" if "week" in q else "month" if "month" in q else "all" if "all time" in q or "overall" in q else "today"
        m = re.search(self.INTENTS[0][1], question, re.I)
        if m:
            tools.used.append("get_order")
            return _plain(reports.order_text(self.svc, scope, m.group(1)))
        for intent, pattern, _ in self.INTENTS[1:]:
            if re.search(pattern, q):
                tools.used.append(intent)
                if intent == "holdings":
                    return _plain(reports.holdings_text(self.svc, scope, sub))
                if intent == "positions":
                    return _plain(reports.positions_text(self.svc, scope, sub))
                if intent == "pnl":
                    return _plain(reports.pnl_text(self.svc, scope, period, sub))
                if intent == "rejected":
                    return _plain(reports.orders_text(self.svc, scope, sub, status="REJECTED"))
                if intent == "trades":
                    return _plain(self._trades_text(scope, sub))
                if intent == "pending":
                    return _plain(reports.orders_text(self.svc, scope, sub, status="PENDING"))
                if intent == "orders":
                    return _plain(reports.orders_text(self.svc, scope, sub))
                if intent == "activity":
                    sc = Scope(sub, scope.label) if sub else scope
                    return _plain(reports.today_text(self.svc, sc) + "\n\n" + reports.recent_text(self.svc, scope, sub, 10))
                if intent == "status":
                    return _plain(reports.status_text(self.svc, Scope(sub, scope.label) if sub else scope))
        return ("I can answer questions about account status, open positions, today's orders and trades, a specific "
                "order (give its order id), rejected or pending orders, P&L (today / week / month) and recent activity.")

    def _mentioned_accounts(self, question: str, scope: Scope) -> list[str]:
        q = question.lower()
        out = []
        for a in self.svc.oms.accounts(scope.account_ids):
            name = (a["client_name"] or "").lower()
            first = name.split()[0] if name else ""
            if a["account_id"].lower() in q or (first and len(first) > 2 and re.search(rf"\b{re.escape(first)}\b", q)):
                out.append(a["account_id"])
        return out

    def _trades_text(self, scope: Scope, sub: list[str] | None) -> str:
        rows = self.svc.oms.fills(sub or scope.account_ids, day=today_ist(), limit=25)
        if not rows:
            return "💹 No trades executed today."
        lines = [f"💹 <b>Trades today</b> ({len(rows)})", ""]
        for f in rows:
            lines.append(f"{h(f['filled_at'][11:19])} <b>{h(f['side'])}</b> {h(f['symbol'])} {f['quantity']} @ "
                         f"{f['price']:,.2f} · {h(f['client_name'])} <code>{h(mask_account(f['account_id']))}</code>")
        return "\n".join(lines)


def _plain(html_text: str) -> str:
    """Rules-engine output is Telegram HTML; the API returns plain text everywhere."""
    t = re.sub(r"</?(b|i|code)>", "", html_text)
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def recent_cutoff(minutes: int) -> str:
    return (now_ist() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
