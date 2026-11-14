"""The TradePulse Telegram bot: commands, account linking, group registration and Q&A.

Authorization model
-------------------
* **Private chat**: a Telegram user is anonymous until linked to an OMS login. The OMS user
  creates a one-time code in the dashboard (Telegram -> "Link my Telegram") and sends
  ``/link CODE`` to the bot. From then on the bot answers with exactly that OMS user's
  visibility (ADMIN all, MANAGER their book, CLIENT their own accounts).
* **Group chat**: the group's scope is the accounts of the TradePulse monitoring groups the chat
  is attached to - the same accounts whose updates the group already receives. Members can ask
  about those accounts (if the group allows queries) and nothing else, whoever they are.
* Adding the bot to a group only registers it as PENDING. An ADMIN activates it (dashboard, or
  ``/register`` from a linked ADMIN in that group) and attaches it to monitoring groups.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta

from . import bus as busmod
from . import reports, tgui
from .config import IST, ist_iso, now_ist
from .notify.format import h, split_text
from .service import Scope

log = logging.getLogger("tradepulse.bot")

COMMANDS = [
    ("menu", "⚡ Open the control panel"), ("help", "What I can do"), ("status", "Account status and health"), ("today", "Today's orders, trades and P&L"),
    ("positions", "Open positions"), ("holdings", "Holdings"), ("refresh", "Re-read from broker: /refresh positions|holdings ACCOUNT"), ("orders", "Today's orders"), ("pending", "Pending / working orders"),
    ("rejected", "Rejected orders today"), ("trades", "Executed trades today"), ("pnl", "P&L: /pnl [week|month]"),
    ("recent", "Recent activity"), ("account", "One account at a glance: /account NAME"), ("order", "Order lifecycle: /order <id>"), ("accounts", "Accounts I can show you"),
    ("groups", "Monitoring groups"),
    ("link", "Link your OMS login: /link CODE"), ("whoami", "Who you are to TradePulse"),
]

HELP = """<b>TradePulse</b> – trading monitor for the SkyMatrix OMS

/menu – ⚡ control panel with buttons (start here)
/status – account status and health
/today – today's orders, trades and P&amp;L
/positions – open positions · /holdings – holdings
/refresh positions|holdings ACCOUNT – re-read from the broker (1 request / 3 s)
/orders · /pending · /rejected · /trades
/pnl [week|month] – P&amp;L
/recent – recent activity
/account NAME – one account at a glance
/order &lt;id&gt; – one order's lifecycle
/accounts · /groups
{group_hint}

Private chat: link your OMS login first with /link CODE (create the code in the TradePulse dashboard).
TradePulse is read-only: it never places or changes orders."""


def new_link_code() -> str:
    return "-".join(secrets.token_hex(2).upper() for _ in range(3))


class Bot:
    def __init__(self, store, svc, telegram_factory, bus=None, reconciler=None):
        self.store, self.svc = store, svc
        self.telegram_factory = telegram_factory
        self.bus = bus
        self.reconciler = reconciler
        self.username = ""
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=6))
        self._rate: dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self.stats = {"updates": 0, "replies": 0, "last_update_at": "", "last_error": "", "username": ""}

    # --- helpers ------------------------------------------------------------------------------
    def tg(self):
        return self.telegram_factory()

    def reply(self, chat_id, text: str, reply_to: int | None = None, markup: dict | None = None) -> None:
        tg = self.tg()
        if tg is None:
            return
        parts = split_text(text)
        for i, part in enumerate(parts):
            try:
                tg.send_message(chat_id, part, reply_to=reply_to, reply_markup=markup if i == len(parts) - 1 else None)
                self.stats["replies"] += 1
            except Exception as ex:
                self.stats["last_error"] = str(ex)[:200]
                log.warning("reply to %s failed: %s", chat_id, ex)
            reply_to = None

    def _limited(self, key: str, per_min: int = 20) -> bool:
        q = self._rate[key]
        now = time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= per_min:
            return True
        q.append(now)
        return False

    def _touch_user(self, frm: dict) -> dict:
        uid = str(frm.get("id"))
        self.store.execute("""INSERT INTO tg_users (telegram_user_id, username, first_name, last_seen) VALUES (?, ?, ?, ?)
                              ON CONFLICT(telegram_user_id) DO UPDATE SET username = excluded.username,
                              first_name = excluded.first_name, last_seen = excluded.last_seen""",
                           (uid, frm.get("username") or "", frm.get("first_name") or "", ist_iso()))
        return self.store.one("SELECT * FROM tg_users WHERE telegram_user_id = ?", (uid,))

    def oms_user(self, tg_user: dict) -> dict | None:
        if not tg_user or not tg_user.get("oms_user_id") or tg_user.get("blocked"):
            return None
        u = self.svc.oms.user_by_id(int(tg_user["oms_user_id"]))
        return u if u and u["status"] == "ACTIVE" else None

    def chat_scope(self, chat: dict, tg_user: dict) -> tuple[Scope | None, str]:
        if chat.get("type") == "private":
            u = self.oms_user(tg_user)
            if not u:
                return None, ("Your Telegram account isn't linked to TradePulse yet.\n\nIn the TradePulse dashboard open "
                              "<b>Telegram → Link my Telegram</b>, then send <code>/link CODE</code> here.")
            return self.svc.scope_for_user(u), ""
        ch = self.store.channel_by_chat("telegram", str(chat["id"]))
        if not ch or ch["status"] != "ACTIVE" or not ch["enabled"]:
            return None, (f"This group isn't connected to TradePulse yet (chat id <code>{h(chat['id'])}</code>). "
                          "An admin can activate it in the dashboard under Telegram → Chats.")
        groups = [g for g in self.store.groups_for_channel(ch["id"]) if g["enabled"]]
        if not groups:
            return None, "This chat is active but not attached to any monitoring group yet."
        allowed = [g for g in groups if g["allow_queries"]]
        if not allowed:
            return None, "Questions are turned off for this group. Updates will still be posted here."
        return self.svc.scope_for_groups(allowed, user=self.oms_user(tg_user)), ""

    # --- update entry point ----------------------------------------------------------------------
    def handle_update(self, update: dict) -> None:
        self.stats["updates"] += 1
        self.stats["last_update_at"] = ist_iso()
        try:
            if "my_chat_member" in update:
                self._membership(update["my_chat_member"])
            msg = update.get("message")
            if msg:
                self._message(msg)
            if update.get("callback_query"):
                self._callback(update["callback_query"])
        except Exception as ex:
            self.stats["last_error"] = f"{type(ex).__name__}: {ex}"[:200]
            log.exception("update handling failed")

    def _membership(self, m: dict) -> None:
        chat = m.get("chat") or {}
        if chat.get("type") == "private":
            return
        status = (m.get("new_chat_member") or {}).get("status")
        title = chat.get("title") or ""
        if status in ("member", "administrator"):
            existing = self.store.channel_by_chat("telegram", str(chat["id"]))
            self.store.upsert_channel("telegram", str(chat["id"]), title=title, chat_type=chat.get("type", ""),
                                      status=None if existing and existing["status"] == "ACTIVE" else "PENDING")
            self.store.audit(f"tg:{(m.get('from') or {}).get('id')}", "BOT_ADDED_TO_CHAT", title, str(chat["id"]))
        elif status in ("left", "kicked"):
            if self.store.channel_by_chat("telegram", str(chat["id"])):
                self.store.upsert_channel("telegram", str(chat["id"]), enabled=False, status="REMOVED")
            self.store.audit(f"tg:{(m.get('from') or {}).get('id')}", "BOT_REMOVED_FROM_CHAT", title, str(chat["id"]))

    def _message(self, msg: dict) -> None:
        chat = msg.get("chat") or {}
        frm = msg.get("from") or {}
        if msg.get("migrate_to_chat_id") or msg.get("migrate_from_chat_id"):
            old, new = ((chat["id"], msg["migrate_to_chat_id"]) if msg.get("migrate_to_chat_id")
                        else (msg["migrate_from_chat_id"], chat["id"]))
            self.store.migrate_channel("telegram", str(old), str(new))
            return
        text = (msg.get("text") or "").strip()
        if not text or frm.get("is_bot"):
            return
        tg_user = self._touch_user(frm)
        if chat.get("type") != "private":
            self.store.upsert_channel("telegram", str(chat["id"]), title=chat.get("title") or "",
                                      chat_type=chat.get("type", ""))
        mid = msg.get("message_id")
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            cmd = cmd.split("@")[0].lower()
            target = text[1:].split(" ")[0]
            if "@" in target and self.username and target.split("@")[1].lower() != self.username.lower():
                return                                                # a command for another bot
            return self._command(cmd, arg.strip(), chat, tg_user, mid)
        if chat.get("type") == "private":
            return self._hint(chat, mid)
        mentioned = self.username and f"@{self.username.lower()}" in text.lower()
        replied = ((msg.get("reply_to_message") or {}).get("from") or {}).get("username", "").lower() == self.username.lower() \
            if self.username else False
        if mentioned or replied:
            return self._hint(chat, mid)

    # --- commands ------------------------------------------------------------------------------------
    def _command(self, cmd: str, arg: str, chat: dict, tg_user: dict, mid) -> None:
        cid = chat["id"]
        private = chat.get("type") == "private"
        if cmd == "help":
            return self.reply(cid, HELP.format(group_hint=""), markup=tgui.kb([tgui.btn("⚡ Open menu", "home")]))
        if cmd == "chatid":
            return self.reply(cid, f"Chat id: <code>{h(cid)}</code>")
        if cmd == "link":
            return self._link(arg, chat, tg_user)
        if cmd == "unlink":
            if not private:
                return self.reply(cid, "Use /unlink in a private chat with me.")
            self.store.execute("UPDATE tg_users SET oms_user_id = NULL, linked_at = '' WHERE telegram_user_id = ?",
                               (tg_user["telegram_user_id"],))
            self.store.audit(f"tg:{tg_user['telegram_user_id']}", "TELEGRAM_UNLINKED")
            return self.reply(cid, "Unlinked. I won't show you any account data until you link again.")
        if cmd == "whoami":
            u = self.oms_user(tg_user)
            return self.reply(cid, f"You are linked to <b>{h(u['name'])}</b> ({h(u['role'])})." if u else
                              "You are not linked to a TradePulse/OMS login.")
        if cmd == "register":
            return self._register(chat, tg_user)
        scope, why = self.chat_scope(chat, tg_user)
        if scope is None:
            return self.reply(cid, why)
        if self._limited(f"u{tg_user['telegram_user_id']}"):
            return self.reply(cid, "Too many requests - please wait a minute.")
        if cmd == "refresh":
            return self._refresh(arg, chat, tg_user, scope)
        if cmd in tgui.COMMAND_VIEWS:
            view, varg = tgui.COMMAND_VIEWS[cmd]
            acct = None
            if arg and cmd != "pnl":
                ids = self.svc.resolve_account(scope, arg)
                if not ids:
                    return self.reply(cid, f"No account matching <b>{h(arg)}</b> is available here.")
                if len(ids) > 1:
                    text, markup = tgui.render(self.svc, scope, "acc", None, f"{view}.{varg or ''}")
                    return self.reply(cid, text, markup=markup)
                acct = ids[0]
            if cmd == "pnl" and arg.lower() in ("today", "week", "month"):
                varg = arg.lower()
            text, markup = tgui.render(self.svc, scope, view, acct, varg)
            return self.reply(cid, text, markup=markup)
        if cmd == "order" and arg:
            text, markup = tgui.render(self.svc, scope, "od", None, arg.strip())
            return self.reply(cid, text, markup=markup)
        if cmd not in {c for c, _ in COMMANDS}:
            return self.reply(cid, "Unknown command. Send /help for the list.")
        acct_ids = self.svc.resolve_account(scope, arg) if arg and cmd not in ("pnl", "order") else None
        if arg and acct_ids == [] and cmd not in ("pnl", "order"):
            return self.reply(cid, f"No account matching <b>{h(arg)}</b> is available here.")
        sub = scope.narrow(acct_ids) if acct_ids else scope
        if cmd == "status":
            text = reports.status_text(self.svc, sub)
        elif cmd == "today":
            text = reports.today_text(self.svc, sub)
        elif cmd == "positions":
            text = reports.positions_text(self.svc, scope, acct_ids)
        elif cmd == "holdings":
            text = reports.holdings_text(self.svc, scope, acct_ids)
        elif cmd == "orders":
            text = reports.orders_text(self.svc, scope, acct_ids)
        elif cmd == "pending":
            text = reports.orders_text(self.svc, scope, acct_ids, status="PENDING")
        elif cmd == "rejected":
            text = reports.orders_text(self.svc, scope, acct_ids, status="REJECTED")
        elif cmd == "trades":
            text = reports.trades_text(self.svc, scope, acct_ids)
        elif cmd == "pnl":
            period = arg.lower() if arg.lower() in ("today", "week", "month", "all") else "today"
            text = reports.pnl_text(self.svc, scope, period)
        elif cmd == "recent":
            text = reports.recent_text(self.svc, scope, acct_ids)
        elif cmd == "order":
            text = reports.order_text(self.svc, scope, arg) if arg else "Usage: /order &lt;order id&gt;"
        elif cmd == "accounts":
            text = reports.accounts_text(self.svc, scope)
        elif cmd == "groups":
            text = reports.groups_text(self._visible_groups(scope))
        else:
            text = "Unknown command. Send /help for the list."
        self.reply(cid, text)

    def _refresh(self, arg: str, chat: dict, tg_user: dict, scope: Scope) -> None:
        """/refresh positions|holdings <account> - one read-only broker request, global 1-per-3s slot."""
        from .brokers import BrokerReadError, Busy
        cid = chat["id"]
        parts = arg.split(maxsplit=1)
        kind = (parts[0].lower() if parts else "")
        kind = {"position": "positions", "holding": "holdings"}.get(kind, kind)
        if kind not in ("positions", "holdings") or len(parts) < 2:
            return self.reply(cid, "Usage: <code>/refresh positions ACCOUNT</code> or <code>/refresh holdings ACCOUNT</code>\n"
                                   "One broker request every 3 seconds is allowed across all users.")
        ids = self.svc.resolve_account(scope, parts[1])
        if len(ids) != 1:
            return self.reply(cid, "Name exactly one account you can see (client name or last digits)." if ids else
                              f"No account matching <b>{h(parts[1])}</b> is available here.")
        if self.reconciler is None:
            return self.reply(cid, "Refresh is not available in this process.")
        actor = f"tg:{tg_user['telegram_user_id']}"
        try:
            res = self.reconciler.request(ids[0], kind, actor)
        except Busy as b:
            secs = max(1, -(-b.wait_ms // 1000))
            return self.reply(cid, f"⏳ Another broker request is in progress. Please wait {secs} second{'s' if secs != 1 else ''} "
                                   "and try again (1 request every 3 seconds).")
        except BrokerReadError as ex:
            return self.reply(cid, f"Could not read {kind} from the broker: {h(str(ex))}")
        diffs = res["mismatches"]
        head = f"✅ {kind.title()} refreshed from the broker ({res['rows']} rows)."
        if diffs:
            head += f"\n⚠️ {len(diffs)} difference(s) vs the TradePulse book (book → broker):\n" + "\n".join(
                f"{h(d['symbol'] or d['exchange_token'])} {d['product']}: {d['book_qty']} → {d['broker_qty']}" for d in diffs[:15])
        text, markup = tgui.render(self.svc, scope, "pos" if kind == "positions" else "hold", ids[0], banner=head)
        self.reply(cid, text, markup=markup)

    def _callback(self, cq: dict) -> None:
        """A button tap: re-check who/where, then re-render the same message in place."""
        tg = self.tg()
        if tg is None:
            return
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        req = tgui.parse_cb(cq.get("data", ""))
        if not chat or req is None:
            return tg.answer_callback_query(cq["id"], "This button has expired.")
        tg_user = self._touch_user(cq.get("from") or {})
        scope, why = self.chat_scope(chat, tg_user)
        if scope is None:
            return tg.answer_callback_query(cq["id"], re.sub(r"<[^>]+>", "", why)[:190], show_alert=True)
        if self._limited(f"cb{tg_user['telegram_user_id']}", per_min=60):
            return tg.answer_callback_query(cq["id"], "Too many taps - wait a moment.", show_alert=True)
        banner, view = "", req["view"]
        if view == "rk":                                       # refresh from broker: the 3-second slot
            from .brokers import BrokerReadError, Busy
            acct, kind = req["acct"], req["arg"]
            if (not acct or acct not in self.svc.book_ids(scope, [acct]) or kind not in ("positions", "holdings")
                    or self.reconciler is None):
                return tg.answer_callback_query(cq["id"], "Not available.", show_alert=True)
            try:
                res = self.reconciler.request(acct, kind, f"tg:{tg_user['telegram_user_id']}")
            except Busy as b:
                secs = max(1, -(-b.wait_ms // 1000))
                return tg.answer_callback_query(cq["id"], f"⏳ Another broker request is in progress.\nPlease wait {secs} "
                                                          f"second{'s' if secs != 1 else ''} and tap again.", show_alert=True)
            except BrokerReadError as ex:
                return tg.answer_callback_query(cq["id"], f"Broker read failed: {ex}"[:190], show_alert=True)
            diffs = res["mismatches"]
            banner = (f"✅ <b>{kind.title()} refreshed</b> from the broker" +
                      (f" · ⚠️ {len(diffs)} difference(s) vs the book" if diffs else " · book matches"))
            tg.answer_callback_query(cq["id"], "Refreshed ✓")
            view = "pos" if kind == "positions" else "hold"
            text, markup = tgui.render(self.svc, scope, view, acct, None, 0, banner=banner)
        else:
            text, markup = tgui.render(self.svc, scope, view, req["acct"], req["arg"], req["page"])
            tg.answer_callback_query(cq["id"])
        try:
            tg.edit_message_text(chat["id"], msg["message_id"], text[:4096], reply_markup=markup)
        except Exception as ex:
            if "not modified" not in str(ex).lower():          # tapping Update with nothing new is fine
                log.warning("edit failed: %s", ex)
                self.reply(chat["id"], text, markup=markup)    # e.g. the message is too old to edit

    def _visible_groups(self, scope: Scope) -> list[dict]:
        if scope.group_ids:
            return [g for g in self.store.groups() if g["id"] in scope.group_ids]
        allowed = set(scope.account_ids)
        return [g for g in self.store.groups() if g["account_ids"] and set(g["account_ids"]) <= allowed]

    def _link(self, code: str, chat: dict, tg_user: dict) -> None:
        cid = chat["id"]
        if chat.get("type") != "private":
            return self.reply(cid, "For your security, send /link in a private chat with me.")
        code = code.strip().upper()
        if self._limited(f"link{tg_user['telegram_user_id']}", per_min=5):
            return self.reply(cid, "Too many attempts - please wait a minute.")
        row = self.store.one("SELECT * FROM link_codes WHERE code = ?", (code,))
        if not row or row["used_at"] or row["expires_at"] < ist_iso():
            return self.reply(cid, "That code is invalid or has expired. Create a new one in the dashboard.")
        user = self.svc.oms.user_by_id(int(row["oms_user_id"]))
        if not user or user["status"] != "ACTIVE":
            return self.reply(cid, "That OMS login is not active.")
        with self.store.tx() as c:
            c.execute("UPDATE link_codes SET used_at = ?, used_by = ? WHERE code = ?",
                      (ist_iso(), tg_user["telegram_user_id"], code))
            c.execute("UPDATE tg_users SET oms_user_id = ?, linked_at = ? WHERE telegram_user_id = ?",
                      (user["id"], ist_iso(), tg_user["telegram_user_id"]))
        self.store.audit(f"tg:{tg_user['telegram_user_id']}", "TELEGRAM_LINKED", user["username"])
        self.reply(cid, f"✅ Linked to <b>{h(user['name'])}</b> ({h(user['role'])}). Try /status or just ask a question.")

    def _register(self, chat: dict, tg_user: dict) -> None:
        cid = chat["id"]
        if chat.get("type") == "private":
            return self.reply(cid, "Use /register inside the group you want TradePulse to post to.")
        u = self.oms_user(tg_user)
        if not u or u["role"] != "ADMIN":
            return self.reply(cid, f"Only a linked TradePulse admin can activate a group. Chat id: <code>{h(cid)}</code>")
        self.store.upsert_channel("telegram", str(cid), title=chat.get("title") or "", chat_type=chat.get("type", ""),
                                  enabled=True, status="ACTIVE")
        self.store.audit(u["username"], "CHAT_ACTIVATED", chat.get("title") or str(cid), "via /register")
        self.reply(cid, "✅ This group is active in TradePulse. Attach it to a monitoring group in the dashboard "
                        "to start receiving updates.")

    def _hint(self, chat: dict, mid) -> None:
        self.reply(chat["id"], "I answer commands: /status · /today · /positions · /holdings · /orders · /trades · /pnl · "
                               "/recent · /order &lt;id&gt; · /refresh positions|holdings ACCOUNT. Send /help for details.",
                   reply_to=mid)

    # --- polling loop ------------------------------------------------------------------------------
    def run_polling(self, stop: threading.Event) -> None:
        log.info("Telegram polling started")
        offset = int(self.store.cursor("tg_offset", "0") or 0) or None
        backoff = 1
        while not stop.is_set():
            tg = self.tg()
            if tg is None:
                self.stats["last_error"] = "bot token not configured"
                self._health(False)
                stop.wait(10)
                continue
            try:
                if not self.username:
                    me = tg.get_me()
                    self.username = me.get("username", "")
                    self.stats["username"] = self.username
                    try:
                        tg.set_commands(COMMANDS)
                    except Exception:
                        pass
                updates = tg.get_updates(offset, timeout=25)
                for u in updates:
                    offset = int(u["update_id"]) + 1
                    self.store.set_cursor("tg_offset", offset)
                    self.handle_update(u)
                backoff = 1
                self._health(True)
            except Exception as ex:
                self.stats["last_error"] = f"{type(ex).__name__}: {ex}"[:200]
                self._health(False)
                log.warning("Telegram polling error: %s", ex)
                stop.wait(backoff)
                backoff = min(60, backoff * 2)

    def _health(self, ok: bool) -> None:
        if self.bus is None:
            return
        try:
            self.bus.hset(busmod.HEALTH, "telegram_bot", json.dumps({**self.stats, "ok": ok, "at": ist_iso()}))
        except Exception:
            pass


def create_link_code(store, oms_user_id: int, minutes: int = 15) -> dict:
    code = new_link_code()
    exp = (now_ist() + timedelta(minutes=minutes)).astimezone(IST).isoformat(timespec="seconds")
    store.execute("DELETE FROM link_codes WHERE oms_user_id = ? AND used_at = ''", (oms_user_id,))
    store.execute("INSERT INTO link_codes (code, oms_user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                  (code, oms_user_id, ist_iso(), exp))
    return {"code": code, "expires_at": exp}
