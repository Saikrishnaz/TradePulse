"""'Final only' order updates (the default): one Telegram message per order, sent when it ends, carrying
every execution - instead of one message per exchange fill (production: 14 'Trade executed' for one order)."""

import random
from datetime import datetime, timedelta

from conftest import INSTRUMENTS, pump, setup_group, start_live
from scripts.oms_redis_sim import IST
from tradepulse.config import ist_iso, now_ist


def notifs(rt):
    return rt.store.rows("SELECT n.*, e.type FROM notifications n JOIN events e ON e.event_id = n.event_id ORDER BY n.id")


def deliver_now(rt):
    """Skip the few seconds a final message waits for late fills."""
    rt.store.execute("UPDATE notifications SET next_attempt_at = ? WHERE status = 'QUEUED'",
                     (ist_iso(now_ist() - timedelta(seconds=1)),))
    return rt.notifier.run_once()


def fill_in_pieces(rt, account, pieces, final_status="COMPLETE", message=""):
    sim = rt.sim
    o = sim.order(account, outcome="open", instrument=INSTRUMENTS[0], lots=5)
    done = 0
    for q, px in pieces:
        done += q
        sim._evt(account, "FILL", {"internal_order_id": o["internal_order_id"], "fill_id": f"F{random.randint(1, 10**9)}",
                                   "quantity": q, "price": px, "filled_at": datetime.now(IST).isoformat(timespec="milliseconds"),
                                   "broker_order_id": o["broker_order_id"], "exchange_trade_id": str(random.randint(10**8, 10**9)),
                                   "source": "BROKER_TRADEBOOK"})
        sim._evt(account, "ORDER_STATUS", {"internal_order_id": o["internal_order_id"], "status": "PARTIALLY_FILLED",
                                           "filled_quantity": done, "broker_order_id": o["broker_order_id"], "source": "BROKER_POLL"})
    sim._evt(account, "ALERT", {"level": "warning", "message": "re-priced 3 times", "internal_order_id": o["internal_order_id"]})
    sim._evt(account, "ORDER_STATUS", {"internal_order_id": o["internal_order_id"], "status": final_status, "filled_quantity": done,
                                       "broker_order_id": o["broker_order_id"], "message": message, "source": "BROKER_POLL"})
    return o


def test_five_lots_filled_in_pieces_is_one_message(rt, tg):
    start_live(rt)
    setup_group(rt, ["ZM4521"])
    fill_in_pieces(rt, "ZM4521", [(75, 125.10), (150, 125.20), (75, 125.00), (75, 125.15)], final_status="PARTIALLY_FILLED")
    pump(rt)
    assert notifs(rt) == []                                   # placed / each fill / alert: recorded, not sent
    assert rt.store.one("SELECT COUNT(*) AS n FROM events WHERE type = 'TRADE_EXECUTED'")["n"] == 4   # still on the dashboard
    o = rt.store.one("SELECT internal_order_id, broker_order_id FROM orders")
    rt.sim._evt("ZM4521", "ORDER_STATUS", {"internal_order_id": o["internal_order_id"], "status": "COMPLETE", "filled_quantity": 375,
                                           "average_price": 125.13, "broker_order_id": o["broker_order_id"], "source": "BROKER_POLL"})
    pump(rt)
    rows = notifs(rt)
    assert [r["type"] for r in rows] == ["ORDER_FILLED"] and rows[0]["kind"] == "order_final"
    assert rt.notifier.run_once() == 0                        # held a few seconds so late fills are included
    deliver_now(rt)
    assert len(tg.sent) == 1
    t = tg.sent[0]["text"]
    assert "✅ <b>Order filled</b>" in t and "Filled <b>375/375</b>" in t and "4 executions" in t
    assert "125.00 – 125.20" in t and "<blockquote expandable>" in t and "re-priced 3 times" in t
    assert "Position now: <b>LONG 375</b>" in t


def test_cancel_after_partial_fill_is_one_message(rt, tg):
    start_live(rt)
    setup_group(rt, ["ZM4521"])
    fill_in_pieces(rt, "ZM4521", [(75, 125.10), (75, 125.20)], final_status="CANCELLED",
                   message="cancel requested after 50 re-price attempts without a full fill")
    pump(rt)
    assert [r["type"] for r in notifs(rt)] == ["ORDER_CANCELLED"]
    deliver_now(rt)
    t = tg.sent[-1]["text"]
    assert "partly filled" in t and "Filled <b>150/375</b>" in t and "re-price attempts" in t


def test_every_step_setting_restores_step_by_step(rt, tg):
    from conftest import every_step
    start_live(rt)
    setup_group(rt, ["ZM4521"])
    every_step(rt)
    fill_in_pieces(rt, "ZM4521", [(75, 125.10), (75, 125.20)])
    pump(rt)
    types = [r["type"] for r in notifs(rt)]
    assert types.count("ORDER_PARTIALLY_FILLED") >= 1 and "ORDER_FILLED" in types and len(types) > 2


def test_setting_validated(rt, client):
    from conftest import H, login
    login(client, "admin", "admin123")
    assert client.put("/api/settings", json={"order_alerts": "every"}, headers=H).status_code == 200
    assert client.put("/api/settings", json={"order_alerts": "sometimes"}, headers=H).status_code == 400
    assert client.get("/api/notifications/settings", headers=H).json()["settings"]["order_alerts"] == "every"
