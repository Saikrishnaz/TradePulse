"""Notification delivery worker: drains the SQLite outbox, never blocks ingestion.

Retry policy: exponential backoff ``retry_base_seconds * 2^(attempt-1)`` (capped at 1 hour), up
to ``retry_max_attempts``; then DEAD (visible in Notifications -> History, retryable by an admin).
A Telegram 429 waits exactly ``retry_after``. A permanent error (bot removed from the group, chat
not found) goes straight to DEAD and marks the channel ERROR so the dashboard shows why.
Batched groups: everything due for one channel is joined into as few messages as fit.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta

from .. import bus as busmod
from ..config import IST, ist_iso, now_ist
from .format import batch_text, split_text

log = logging.getLogger("tradepulse.notifier")


class Notifier:
    def __init__(self, store, bus, telegram_factory, whatsapp=None):
        self.store, self.bus = store, bus
        self.telegram_factory = telegram_factory        # () -> TelegramClient | None
        self.whatsapp = whatsapp
        self.stats = {"sent": 0, "failed": 0, "dead": 0, "last_sent_at": "", "last_error": ""}

    def due(self, limit: int = 100) -> list[dict]:
        return self.store.rows("""SELECT n.*, c.kind AS provider, c.chat_id, c.enabled AS channel_enabled, c.status AS channel_status,
                                         c.title AS channel_title
                                    FROM notifications n JOIN channels c ON c.id = n.channel_id
                                   WHERE n.status IN ('QUEUED','FAILED') AND n.next_attempt_at <= ?
                                   ORDER BY n.id LIMIT ?""", (ist_iso(), limit))

    def run_once(self) -> int:
        rows = self.due()
        if not rows:
            return 0
        settings = self.store.get_settings()
        sent = 0
        # Batched rows for one channel go out together; instant rows one by one, in order.
        batches: dict[int, list[dict]] = {}
        singles: list[dict] = []
        for r in rows:
            (batches.setdefault(r["channel_id"], []) if r["batched"] else singles).append(r)
        for r in singles:
            sent += self._deliver([r], settings)
        for _cid, group in batches.items():
            sent += self._deliver(group, settings)
        return sent

    def _deliver(self, rows: list[dict], settings: dict) -> int:
        ch = rows[0]
        ids = [r["id"] for r in rows]
        if not ch["channel_enabled"] or ch["channel_status"] != "ACTIVE":
            self._mark(ids, "SKIPPED", error="channel disabled")
            return 0
        if not settings.get(f"{ch['provider']}_enabled", ch["provider"] == "telegram"):
            self._mark(ids, "SKIPPED", error=f"{ch['provider']} disabled globally")
            return 0
        for r in rows:
            if r.get("kind") == "order_final":
                from .format import order_final_text
                try:
                    txt = order_final_text(self.store, r["event_id"])
                except Exception:
                    log.exception("final order summary failed; sending the plain card")
                    txt = ""
                if txt:
                    r["text"] = txt
                    self.store.execute("UPDATE notifications SET text = ? WHERE id = ?", (txt, r["id"]))
        texts = batch_text([r["text"] for r in rows]) if len(rows) > 1 else split_text(rows[0]["text"])
        try:
            ref = ""
            if ch["provider"] == "telegram":
                tg = self.telegram_factory()
                if tg is None:
                    raise _Deferred("Telegram bot token not configured")
                markup = self._markup(rows)
                for i, t in enumerate(texts):
                    res = tg.send_message(ch["chat_id"], t, reply_markup=markup if i == len(texts) - 1 else None)
                    ref = str((res or {}).get("message_id") or ref)
                    time.sleep(0.05)
            elif ch["provider"] == "whatsapp":
                if self.whatsapp is None or not self.whatsapp.configured():
                    self._mark(ids, "SKIPPED", error="WhatsApp not configured")
                    return 0
                for t in texts:
                    ref = self.whatsapp.send(ch["chat_id"], t, template=str(settings.get("whatsapp_template") or ""))
            self._mark(ids, "SENT", ref=ref)
            self.stats["sent"] += len(ids)
            self.stats["last_sent_at"] = ist_iso()
            return len(ids)
        except _Deferred as ex:
            self._retry(rows, settings, str(ex), delay=30)
        except Exception as ex:                             # TelegramError / WhatsAppError / anything else
            permanent = bool(getattr(ex, "permanent", False))
            msg = f"{type(ex).__name__}: {ex}"[:500]
            self.stats["last_error"] = msg
            if permanent:
                self._mark(ids, "DEAD", error=msg)
                self.stats["dead"] += len(ids)
                self.store.execute("UPDATE channels SET status = 'ERROR' WHERE id = ?", (ch["channel_id"],))
                self.store.audit("system", "CHANNEL_ERROR", ch["channel_title"] or ch["chat_id"], msg)
            else:
                self._retry(rows, settings, msg, delay=getattr(ex, "retry_after", None))
        self._health()
        return 0

    def _markup(self, rows: list[dict]) -> dict | None:
        """Buttons under a card: order/positions for a single event alert, the menu for summaries and batches."""
        from .. import tgui
        if len(rows) == 1 and rows[0].get("event_id"):
            e = self.store.one("SELECT event_id, type, account_id, internal_order_id FROM events WHERE event_id = ?",
                               (rows[0]["event_id"],))
            if e:
                return tgui.event_keyboard(e)
        if rows and rows[0].get("kind") == "test":
            return None
        return tgui.kb([tgui.btn("⚡ Open menu", "home")])

    def _retry(self, rows, settings, error: str, delay=None) -> None:
        max_attempts = int(settings.get("retry_max_attempts") or 8)
        base = int(settings.get("retry_base_seconds") or 5)
        with self.store.tx() as c:
            for r in rows:
                attempts = int(r["attempts"]) + 1
                if attempts >= max_attempts:
                    c.execute("UPDATE notifications SET status = 'DEAD', attempts = ?, last_error = ? WHERE id = ?",
                              (attempts, error, r["id"]))
                    self.stats["dead"] += 1
                    continue
                wait = int(delay) if delay else min(3600, base * 2 ** (attempts - 1))
                nxt = (now_ist() + timedelta(seconds=wait)).astimezone(IST).isoformat(timespec="seconds")
                c.execute("UPDATE notifications SET status = 'FAILED', attempts = ?, last_error = ?, next_attempt_at = ? "
                          "WHERE id = ?", (attempts, error, nxt, r["id"]))
                self.stats["failed"] += 1

    def _mark(self, ids: list[int], status: str, ref: str = "", error: str = "") -> None:
        now = ist_iso()
        with self.store.tx() as c:
            for i in ids:
                c.execute("""UPDATE notifications SET status = ?, provider_ref = ?, last_error = ?,
                                 sent_at = CASE WHEN ? = 'SENT' THEN ? ELSE sent_at END,
                                 attempts = attempts + CASE WHEN ? IN ('SENT','DEAD') THEN 1 ELSE 0 END
                              WHERE id = ?""", (status, ref, error, status, now, status, i))

    def run(self, stop: threading.Event) -> None:
        log.info("notifier started")
        while not stop.is_set():
            try:
                n = self.run_once()
            except Exception:
                log.exception("notifier loop error")
                n = 0
            self._health()
            stop.wait(0.2 if n else 1.0)

    def _health(self) -> None:
        try:
            q = self.store.one("SELECT COUNT(*) AS n FROM notifications WHERE status IN ('QUEUED','FAILED')")
            self.bus.hset(busmod.HEALTH, "notifier", json.dumps({**self.stats, "queue": q["n"], "at": ist_iso()}))
        except Exception:
            pass


class _Deferred(Exception):
    pass


def queue_message(store, channel_id: int, text: str, kind: str = "digest", group_id: int | None = None) -> int:
    """Queue a non-event message (digest, test, position report) for delivery through the outbox."""
    now = ist_iso()
    return store.execute("""INSERT INTO notifications (event_id, kind, channel_id, group_id, text, status, next_attempt_at,
                                created_at) VALUES (NULL, ?, ?, ?, ?, 'QUEUED', ?, ?)""",
                         (kind, channel_id, group_id, text, now, now))
