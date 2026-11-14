"""OMS (via Redis only) -> TradePulse: roster, order lifecycle, fills, dedupe, ordering, backfill, notifications."""

import json

from conftest import INSTRUMENTS, pump, setup_group, start_live
from tradepulse import bus as busmod
from tradepulse import events as ev
from tradepulse.processor import enqueue

NIFTY_CE, RELIANCE = INSTRUMENTS[0], INSTRUMENTS[3]


def types_for(rt, account=None):
    sql = "SELECT type FROM events" + (" WHERE account_id = ?" if account else "") + " ORDER BY id"
    return [r["type"] for r in rt.store.rows(sql, (account,) if account else ())]


def notifs(rt):
    return rt.store.rows("SELECT n.*, e.type FROM notifications n LEFT JOIN events e ON e.event_id = n.event_id ORDER BY n.id")


def test_roster_sync_keeps_no_credentials(rt):
    accts = {a["account_id"]: a for a in rt.oms.accounts()}
    assert {"ZM4521", "AM88213", "J252505", "BR77104", "BRP0091", "KS50310"} <= set(accts)
    assert accts["J252505"]["client_name"] == "Jayesh Mehta" and accts["J252505"]["broker"] == "angel"
    assert accts["ZM4521"]["username"] == "mathew" and accts["ZM4521"]["session_status"] == "OK"
    raw = rt.store.path.read_bytes()
    for secret in (b"SECRET-ZM4521", b"KEY-ZM4521", b"TOKEN-ZM4521", b"JBSWY3DPEHPK3PXP"):
        assert secret not in raw                                    # roster secrets never reach TradePulse's DB


def test_new_roster_account_appears_without_code(rt):
    roster = json.loads(rt.bus.get_any("smx:roster"))
    roster.append({"account_id": "NEW9999", "name": "New Client", "broker": "zerodha", "username": "newc", "api_key": "x"})
    rt.bus.set_foreign("smx:roster", json.dumps(roster))
    rt.tail.sync_roster()
    assert rt.oms.account("NEW9999")["client_name"] == "New Client"


def test_full_lifecycle_from_streams(rt):
    start_live(rt)
    r = rt.sim.order("ZM4521", outcome="fill", instrument=NIFTY_CE)
    pump(rt)
    t = types_for(rt, "ZM4521")
    assert {"ORDER_PLACED", "ORDER_OPEN", "ORDER_FILLED", "TRADE_EXECUTED", "POSITION_UPDATED"} <= set(t)
    o = rt.store.one("SELECT * FROM orders WHERE internal_order_id = ?", (r["internal_order_id"],))
    assert o["status"] == "COMPLETE" and o["filled_quantity"] == 75 and o["symbol"] == "NIFTY26NOV26000CE"
    assert o["broker_order_id"] == r["broker_order_id"] and o["trading_mode"] == "LIVE"
    life = [x["to_status"] for x in rt.store.rows("SELECT to_status FROM order_events WHERE internal_order_id = ? ORDER BY id",
                                                   (r["internal_order_id"],))]
    assert life == ["ROUTED", "SUBMITTING", "OPEN", "COMPLETE"]
    e = rt.store.one("SELECT * FROM events WHERE type = 'ORDER_FILLED'")
    assert e["client_name"] == "Mathew" and e["filled_quantity"] == 75


def test_partial_reject_cancel(rt):
    start_live(rt)
    rt.sim.order("J252505", outcome="partial", instrument=NIFTY_CE)
    rt.sim.order("J252505", outcome="reject", reject_message="RMS: insufficient margin")
    rt.sim.order("J252505", outcome="cancel")
    pump(rt)
    t = set(types_for(rt, "J252505"))
    assert {"ORDER_PARTIALLY_FILLED", "ORDER_REJECTED", "ORDER_CANCELLED"} <= t
    assert "insufficient margin" in rt.store.one("SELECT message FROM events WHERE type = 'ORDER_REJECTED'")["message"]


def test_cursor_reset_never_duplicates(rt):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="fill")
    pump(rt)
    n = len(types_for(rt))
    for c in rt.store.rows("SELECT name FROM cursors WHERE name LIKE 'stream:%'"):
        rt.store.set_cursor(c["name"], "0-0")                          # crash lost the cursors: re-read everything
    pump(rt)
    assert len(types_for(rt)) == n
    assert rt.store.one("SELECT COUNT(*) AS n FROM fills")["n"] == 1


def test_event_before_command_is_parked_then_applied(rt):
    start_live(rt)
    # worker report arrives on the events stream before TradePulse has read the EXECUTE command
    rt.bus.add_foreign("smx:oms:events", {"type": "ORDER_ACCEPTED", "data": json.dumps(
        {"worker_id": "w-zm4521", "account_id": "ZM4521", "internal_order_id": "OLATE1", "broker_order_id": "999"})})
    rt.tail._drain("smx:oms:events", rt.tail._on_event)
    assert rt.store.one("SELECT COUNT(*) AS n FROM pending_events")["n"] == 1
    rt.bus.add_foreign("smx:oms:cmd:w-zm4521", {"type": "EXECUTE", "data": json.dumps(
        {"execution_id": "X1", "account_id": "ZM4521", "broker": "zerodha", "trading_mode": "LIVE",
         "orders": [{"internal_order_id": "OLATE1", "exchange": "NSE", "exchange_token": 2885, "symbol": "RELIANCE-EQ",
                     "side": "BUY", "product": "CNC", "order_type": "LIMIT", "ordered_quantity": 1, "price": 1300}]})})
    pump(rt)
    assert rt.store.one("SELECT status, broker_order_id FROM orders WHERE internal_order_id = 'OLATE1'") == \
        {"status": "OPEN", "broker_order_id": "999"}
    assert rt.store.one("SELECT COUNT(*) AS n FROM pending_events")["n"] == 0


def test_status_only_moves_forward_and_terminal_is_final(rt):
    start_live(rt)
    r = rt.sim.order("ZM4521", outcome="partial", instrument=NIFTY_CE)
    pump(rt)
    w = rt.sim.w
    w.xadd("smx:oms:events", "ORDER_ACCEPTED", {"account_id": "ZM4521", "internal_order_id": r["internal_order_id"], "broker_order_id": r["broker_order_id"]})
    pump(rt)
    assert rt.store.one("SELECT status FROM orders WHERE internal_order_id = ?", (r["internal_order_id"],))["status"] == "PARTIALLY_FILLED"
    w.xadd("smx:oms:events", "ORDER_STATUS", {"account_id": "ZM4521", "internal_order_id": r["internal_order_id"], "status": "CANCELLED", "filled_quantity": 37})
    w.xadd("smx:oms:events", "ORDER_STATUS", {"account_id": "ZM4521", "internal_order_id": r["internal_order_id"], "status": "OPEN", "filled_quantity": 37})
    pump(rt)
    assert rt.store.one("SELECT status FROM orders WHERE internal_order_id = ?", (r["internal_order_id"],))["status"] == "CANCELLED"


def test_group_rejected_and_worker_alert_and_heartbeat(rt):
    start_live(rt)
    w = rt.sim.w
    w.xadd("smx:oms:cmd:w-j252505", "EXECUTE", {"execution_id": "XG1", "account_id": "J252505", "broker": "angel", "trading_mode": "LIVE",
                                               "orders": [{"internal_order_id": "OG1", "exchange": "NFO", "exchange_token": 1, "symbol": "S",
                                                           "side": "SELL", "product": "NRML", "ordered_quantity": 75}]})
    w.xadd("smx:oms:events", "GROUP_REJECTED", {"account_id": "J252505", "execution_id": "XG1", "code": "INSUFFICIENT_MARGIN", "reason": "short"})
    w.xadd("smx:oms:events", "ALERT", {"account_id": "J252505", "level": "error", "message": "exit failed"})
    pump(rt)
    assert rt.store.one("SELECT status FROM orders WHERE internal_order_id = 'OG1'")["status"] == "REJECTED"
    assert rt.store.one("SELECT message FROM events WHERE type = 'ACCOUNT_ALERT'")["message"] == "exit failed"
    assert rt.store.one("SELECT status FROM workers WHERE account_id = 'J252505'")["status"] == "READY"


def test_webhook_and_stream_same_fill_one_notification(rt):
    start_live(rt)
    setup_group(rt, ["ZM4521"])
    r = rt.sim.order("ZM4521", outcome="fill", instrument=NIFTY_CE)
    enqueue(rt.bus, ev.new_event(ev.ORDER_FILLED, "WEBHOOK:zerodha", ev.order_key("ZM4521", r["broker_order_id"], ev.ORDER_FILLED, r["filled"]),
                                 account_id="ZM4521", broker="zerodha", order_id=r["broker_order_id"], filled_quantity=r["filled"]))
    pump(rt)
    assert len(rt.store.rows("SELECT * FROM events WHERE type = 'ORDER_FILLED'")) == 1
    assert len([n for n in notifs(rt) if n["type"] == "ORDER_FILLED"]) == 1


def test_first_start_history_is_backfill_not_notified(rt):
    setup_group(rt, ["BR77104"])
    rt.sim.order("BR77104", outcome="reject")                           # happened before TradePulse started
    start_live(rt)
    assert "ORDER_REJECTED" in types_for(rt, "BR77104") and notifs(rt) == []
    rt.sim.order("BR77104", outcome="reject")
    pump(rt)
    assert [n["type"] for n in notifs(rt)] == ["ORDER_PLACED", "ORDER_REJECTED"]


def test_only_monitored_accounts_in_groups_notify(rt):
    start_live(rt)
    setup_group(rt, ["ZM4521"])
    rt.sim.order("ZM4521", outcome="reject")
    rt.sim.order("J252505", outcome="reject")
    pump(rt)
    accts = {rt.store.one("SELECT account_id FROM events WHERE event_id = ?", (n["event_id"],))["account_id"] for n in notifs(rt)}
    assert accts == {"ZM4521"} and "ORDER_REJECTED" in types_for(rt, "J252505")


def test_unacked_ingest_is_reclaimed(rt):
    rt.bus.ensure_group(busmod.INGEST, busmod.INGEST_GROUP)
    enqueue(rt.bus, ev.new_event(ev.ORDER_REJECTED, "TEST", "k1", account_id="ZM4521"))
    rt.bus.read_group(busmod.INGEST, busmod.INGEST_GROUP, "dead", count=10)
    g = rt.bus.groups[(busmod.INGEST, busmod.INGEST_GROUP)]
    for mid, (cons, t, d) in list(g["pending"].items()):
        g["pending"][mid] = (cons, t - 60, d)
    rt.processor.run_once(block_ms=0)
    assert rt.store.one("SELECT COUNT(*) AS n FROM events WHERE dedupe_key = 'k1'")["n"] == 1


def test_tradepulse_writes_only_its_namespace(rt):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="fill")
    pump(rt)
    tp_written = set(rt.bus.streams) - {s for s in rt.bus.streams if s.startswith("smx:")}
    assert all(k.startswith("tradepulse:") for k in tp_written)
    assert all(not s.startswith("smx:") for (s, _g) in rt.bus.groups)   # no consumer group on any OMS stream
