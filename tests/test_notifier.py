"""Outbox delivery: success, backoff, Telegram 429, permanent failures, batching, disabled channels."""

from conftest import every_step, pump, setup_group, start_live

from tradepulse.notify.outbox import queue_message
from tradepulse.notify.telegram import TelegramError


def statuses(rt):
    return [r["status"] for r in rt.store.rows("SELECT status FROM notifications ORDER BY id")]


def test_event_notification_is_sent_with_client_id(rt, tg):
    every_step(rt)                                                            # this test is about each step
    start_live(rt)
    setup_group(rt, ["J252505"], chat_id="-1001")
    rt.sim.order("J252505", outcome="reject")
    pump(rt)
    assert rt.notifier.run_once() == 2
    assert statuses(rt) == ["SENT", "SENT"]
    txt = tg.sent[-1]["text"]
    assert "Order rejected" in txt and "Jayesh Mehta" in txt and "J252505" in txt and "<blockquote>" in txt
    assert "****" not in txt                          # the full client id is shown so readers know which account
    assert tg.sent[-1]["chat_id"] == "-1001"


def test_transient_failure_backs_off_then_succeeds(rt, tg):
    _gid, ch = setup_group(rt, ["ZM4521"])
    queue_message(rt.store, ch, "hello")
    tg.fail = TelegramError(500, "Internal Server Error")
    rt.notifier.run_once()
    n = rt.store.one("SELECT * FROM notifications")
    assert n["status"] == "FAILED" and n["attempts"] == 1 and n["next_attempt_at"] > n["created_at"]
    rt.store.execute("UPDATE notifications SET next_attempt_at = created_at")
    tg.fail = None
    rt.notifier.run_once()
    assert statuses(rt) == ["SENT"]


def test_rate_limit_uses_retry_after(rt, tg):
    _gid, ch = setup_group(rt, ["ZM4521"])
    queue_message(rt.store, ch, "hello")
    tg.fail = TelegramError(429, "Too Many Requests", retry_after=42)
    rt.notifier.run_once()
    n = rt.store.one("SELECT * FROM notifications")
    from datetime import datetime
    gap = (datetime.fromisoformat(n["next_attempt_at"]) - datetime.fromisoformat(n["created_at"])).total_seconds()
    assert 40 <= gap <= 45


def test_permanent_failure_is_dead_and_marks_channel(rt, tg):
    _gid, ch = setup_group(rt, ["ZM4521"])
    queue_message(rt.store, ch, "hello")
    tg.fail = TelegramError(403, "Forbidden: bot was kicked from the supergroup chat")
    rt.notifier.run_once()
    assert statuses(rt) == ["DEAD"]
    assert rt.store.one("SELECT status FROM channels WHERE id = ?", (ch,))["status"] == "ERROR"


def test_max_attempts_goes_dead(rt, tg):
    _gid, ch = setup_group(rt, ["ZM4521"])
    rt.store.put_settings({"retry_max_attempts": 2})
    queue_message(rt.store, ch, "hello")
    tg.fail = TelegramError(0, "network: ConnectError")
    for _ in range(3):
        rt.store.execute("UPDATE notifications SET next_attempt_at = created_at")
        rt.notifier.run_once()
    assert statuses(rt) == ["DEAD"]


def test_batched_rows_go_out_as_one_message(rt, tg):
    every_step(rt)                                                            # this test is about each step
    start_live(rt)
    setup_group(rt, ["ZM4521"], delivery_mode="batched", batch_minutes=5)
    rt.sim.order("ZM4521", outcome="reject")
    rt.sim.order("ZM4521", outcome="cancel")
    pump(rt)
    rt.store.execute("UPDATE notifications SET next_attempt_at = created_at")
    n = rt.notifier.run_once()
    assert n == len(statuses(rt)) >= 3 and set(statuses(rt)) == {"SENT"}
    assert len(tg.sent) == 1 and "updates" in tg.sent[0]["text"]


def test_disabled_channel_skips_and_global_switch(rt, tg):
    _gid, ch = setup_group(rt, ["ZM4521"])
    rt.store.execute("UPDATE channels SET enabled = 0 WHERE id = ?", (ch,))
    queue_message(rt.store, ch, "a")
    rt.notifier.run_once()
    assert statuses(rt) == ["SKIPPED"] and tg.sent == []
    rt.store.execute("UPDATE channels SET enabled = 1 WHERE id = ?", (ch,))
    rt.store.put_settings({"telegram_enabled": False})
    queue_message(rt.store, ch, "b")
    rt.notifier.run_once()
    assert statuses(rt) == ["SKIPPED", "SKIPPED"]


def test_missing_token_defers_instead_of_failing(tmp_path):
    from conftest import make_settings
    from tradepulse.bus import MemoryBus
    from tradepulse.runtime import Runtime
    rt = Runtime(make_settings(tmp_path), bus=MemoryBus())       # no bot token anywhere
    _gid, ch = setup_group(rt, ["ZM4521"])
    queue_message(rt.store, ch, "hello")
    rt.notifier.run_once()
    n = rt.store.one("SELECT * FROM notifications")
    assert n["status"] == "FAILED" and "not configured" in n["last_error"]


def test_whatsapp_unconfigured_skips_without_affecting_telegram(rt, tg):
    _gid, ch = setup_group(rt, ["ZM4521"])
    wa = rt.store.upsert_channel("whatsapp", "+919800000000", title="WA", enabled=True, status="ACTIVE")
    rt.store.put_settings({"whatsapp_enabled": True})
    queue_message(rt.store, wa["id"], "wa")
    queue_message(rt.store, ch, "tg")
    rt.notifier.run_once()
    assert statuses(rt) == ["SKIPPED", "SENT"]


def test_html_is_escaped_in_event_text():
    from tradepulse.notify.format import event_text
    e = {"type": "ORDER_REJECTED", "client_name": "<b>x</b>", "broker": "zerodha", "account_id": "AB12345",
         "symbol": "A&B", "message": "<script>", "occurred_at": "2026-11-13T10:00:00+05:30"}
    t = event_text(e)
    assert "&lt;b&gt;x&lt;/b&gt;" in t and "A&amp;B" in t and "&lt;script&gt;" in t and "<script>" not in t
