"""09:00 / 09:05 reconcile, event-based book between snapshots, 1-request-per-3s slot, user refresh."""

import threading
from datetime import datetime

import pytest

from conftest import H, INSTRUMENTS, login, pump, start_live
from tradepulse.brokers import Busy
from tradepulse.config import IST

NIFTY_CE, RELIANCE = INSTRUMENTS[0], INSTRUMENTS[3]


@pytest.fixture
def fast_slot(rt, monkeypatch):
    """Record slot waits instead of sleeping 3 s per request."""
    waits = []

    def wait_slot(stop=None):
        waits.append(1)
    monkeypatch.setattr(rt.readers, "wait_slot", wait_slot)
    return waits


def at(h, m, day=13):                    # 2026-11-13 is a Friday
    return datetime(2026, 11, day, h, m, tzinfo=IST)


def test_scheduled_runs_at_0900_and_0905_once_each(rt, brokers, fast_slot):
    rt.store.set_monitoring(["ZM4521", "J252505"], True, "t")
    assert rt.reconciler.due(at(8, 59)) == []
    assert rt.reconciler.due(at(9, 0)) == ["09:00"]
    res = rt.reconciler.run_due(now=at(9, 0))
    assert res[0]["status"] == "DONE" and res[0]["snapshots"] == 4 and res[0]["requests"] == 4
    assert rt.reconciler.due(at(9, 2)) == []                                   # 09:00 done, 09:05 not yet
    assert rt.reconciler.due(at(9, 5)) == ["09:05"]
    rt.reconciler.run_due(now=at(9, 5))
    assert rt.reconciler.due(at(9, 30)) == []
    assert rt.reconciler.due(at(9, 0, day=14)) == []                           # Saturday
    assert len(fast_slot) == 8                                                  # every broker request waited for the slot


def test_only_monitored_accounts_are_read(rt, brokers, fast_slot):
    rt.store.set_monitoring(["ZM4521"], True, "t")
    rt.reconciler.run("SCHEDULED 09:00")
    assert {c[0] for c in brokers.calls} == {"ZM4521"}
    assert sorted(c[2] for c in brokers.calls) == ["/portfolio/holdings", "/portfolio/positions"]


def test_baseline_then_event_based_book_then_0905_verifies_clean(rt, brokers, fast_slot):
    start_live(rt)
    rt.store.set_monitoring(["J252505"], True, "t")
    brokers.set("J252505", positions=[{"exchange": "NFO", "token": 56723, "product": "NRML", "qty": 75, "symbol": "NIFTY26NOV26000CE",
                                        "avg": 120.0, "ltp": 125.0}],
                holdings=[{"exchange": "NSE", "token": 2885, "symbol": "RELIANCE-EQ", "qty": 10, "avg": 1300.0}])
    rt.reconciler.run("SCHEDULED 09:00")
    pos = rt.oms.positions(["J252505"], "open")
    assert pos[0]["net_quantity"] == 75 and pos[0]["baseline_source"].startswith("BROKER")
    calls_after_baseline = len(brokers.calls)

    # order updates arrive -> the book follows them with NO broker request
    rt.sim.order("J252505", outcome="fill", side="BUY", instrument=NIFTY_CE)               # +75
    rt.sim.order("J252505", outcome="fill", side="BUY", instrument=RELIANCE, lots=5)       # +5 CNC (position + holding)
    pump(rt)
    assert len(brokers.calls) == calls_after_baseline
    pos = {p["symbol"]: p for p in rt.oms.positions(["J252505"], "open")}
    assert pos["NIFTY26NOV26000CE"]["net_quantity"] == 150 and pos["RELIANCE-EQ"]["net_quantity"] == 5
    hold = rt.oms.holdings(["J252505"])[0]
    assert hold["quantity"] == 15 and hold["t1_quantity"] == 5 and hold["change"] == 5
    types = [r["type"] for r in rt.store.rows("SELECT type FROM events ORDER BY id")]
    assert types.count("POSITION_UPDATED") == 2 and types.count("HOLDING_UPDATED") == 1

    # 09:05: the broker agrees with baseline + fills -> no mismatch
    brokers.set("J252505", positions=[{"exchange": "NFO", "token": 56723, "product": "NRML", "qty": 150, "symbol": "NIFTY26NOV26000CE", "avg": 122.0},
                                      {"exchange": "NSE", "token": 2885, "product": "CNC", "qty": 5, "symbol": "RELIANCE-EQ", "avg": 1310.0}],
                holdings=[{"exchange": "NSE", "token": 2885, "symbol": "RELIANCE-EQ", "qty": 15, "t1": 5, "avg": 1303.0}])
    res = rt.reconciler.run("SCHEDULED 09:05")
    assert res["mismatches"] == 0
    assert rt.store.rows("SELECT * FROM events WHERE type = 'RECONCILE_MISMATCH'") == []


def test_mismatch_detected_and_raised(rt, brokers, fast_slot):
    start_live(rt)
    rt.store.set_monitoring(["ZM4521"], True, "t")
    brokers.set("ZM4521", positions=[{"exchange": "NFO", "token": 56723, "product": "NRML", "qty": 75, "symbol": "NIFTY26NOV26000CE"}])
    rt.reconciler.run("SCHEDULED 09:00")
    # someone squares off at the broker directly (not through the OMS) -> no order event reaches TradePulse
    brokers.set("ZM4521", positions=[{"exchange": "NFO", "token": 56723, "product": "NRML", "qty": 0, "symbol": "NIFTY26NOV26000CE"}])
    res = rt.reconciler.run("SCHEDULED 09:05")
    assert res["mismatches"] == 1
    d = rt.store.one("SELECT * FROM reconcile_diffs")
    assert (d["book_qty"], d["broker_qty"], d["symbol"]) == (75, 0, "NIFTY26NOV26000CE")
    pump(rt)
    m = rt.store.one("SELECT * FROM events WHERE type = 'RECONCILE_MISMATCH'")
    assert m and "75 vs broker 0" in m["message"]
    assert rt.oms.positions(["ZM4521"], "open") == []                          # broker is the new baseline


def test_fill_newer_than_snapshot_is_not_lost_nor_double_counted(rt, brokers, fast_slot):
    start_live(rt)
    rt.store.set_monitoring(["ZM4521"], True, "t")
    rt.sim.order("ZM4521", outcome="fill", instrument=NIFTY_CE)                 # before the snapshot: in the broker's answer
    pump(rt)
    brokers.set("ZM4521", positions=[{"exchange": "NFO", "token": 56723, "product": "NRML", "qty": 75, "symbol": "NIFTY26NOV26000CE"}])
    rt.reconciler.run("SCHEDULED 09:00")
    assert rt.oms.positions(["ZM4521"], "open")[0]["net_quantity"] == 75        # not 150
    rt.sim.order("ZM4521", outcome="fill", instrument=NIFTY_CE)                 # after the snapshot
    pump(rt)
    assert rt.oms.positions(["ZM4521"], "open")[0]["net_quantity"] == 150


def test_user_refresh_obeys_3s_slot_and_says_wait(rt, brokers):
    rt.store.set_monitoring(["ZM4521", "J252505"], True, "t")
    out = rt.reconciler.request("ZM4521", "positions", "admin")
    assert out["rows"] == 0 and len(brokers.calls) == 1
    with pytest.raises(Busy) as b:
        rt.reconciler.request("J252505", "holdings", "admin")                  # any account, any kind: one global slot
    assert 0 < b.value.wait_ms <= 3000
    assert len(brokers.calls) == 1                                              # refused before touching the broker


def test_slot_is_global_and_never_below_3s(rt):
    rt.store.put_settings({"broker_slot_seconds": 1})
    assert rt.readers.slot_ms() == 3000
    ok1, _ = rt.bus.acquire_slot("tradepulse:broker:slot", rt.readers.slot_ms())
    ok2, wait = rt.bus.acquire_slot("tradepulse:broker:slot", rt.readers.slot_ms())
    assert ok1 and not ok2 and wait > 2000


def test_real_wait_slot_spaces_requests(rt, brokers):
    """Without the test shortcut: 3 accounts x 2 kinds must take >= 5 slot intervals."""
    import time
    rt.store.set_monitoring(["ZM4521", "AM88213", "J252505"], True, "t")
    rt.store.put_settings({"broker_slot_seconds": 3})
    rt.readers.slot_ms = lambda: 100                                            # scale 3 s -> 0.1 s for the test
    t0 = time.monotonic()
    rt.reconciler.run("SCHEDULED 09:00")
    assert len(brokers.calls) == 6
    assert time.monotonic() - t0 >= 0.5


def test_slot_counts_from_when_the_broker_answered(rt, brokers):
    """Regression: a slow request must not let the next one reach the broker < slot later.
    (Found in E2E: cold TLS made request #1 arrive 2.4 s late; #2 then landed 0.54 s after it.)"""
    import time
    import httpx
    stamps = []

    def slow(request):
        stamps.append(time.monotonic())
        time.sleep(0.25)                                   # broker/TLS latency
        return brokers.handler(request)
    rt.readers._http = httpx.Client(transport=httpx.MockTransport(slow))
    rt.readers.slot_ms = lambda: 200
    rt.store.set_monitoring(["ZM4521", "AM88213"], True, "t")
    rt.reconciler.run("SCHEDULED 09:00")
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 4 and min(gaps) >= 0.2 + 0.25 - 0.02, gaps


def test_paper_account_read_from_redis_without_broker_or_slot(rt, brokers, fast_slot):
    start_live(rt)
    rt.sim.order("BRP0091", outcome="fill", instrument=RELIANCE, lots=3)
    pump(rt)
    rt.store.set_monitoring(["BRP0091"], True, "t")
    rt.reconciler.run("SCHEDULED 09:00")
    assert brokers.calls == [] and fast_slot == []
    h = rt.oms.holdings(["BRP0091"])
    assert h and h[0]["quantity"] == 3


def test_missing_session_recorded_not_retried(rt, brokers, fast_slot):
    rt.bus.hset_foreign("smx:session:KS50310", {"status": "FAILED", "access_token": ""})
    rt.store.set_monitoring(["KS50310"], True, "t")
    res = rt.reconciler.run("SCHEDULED 09:00")
    assert res["status"] == "FAILED" and res["errors"] == 2 and brokers.calls == []
    f = rt.store.rows("SELECT error FROM snapshots WHERE ok = 0")
    assert len(f) == 2 and "session" in f[0]["error"]


def test_angel_carried_position_uses_cf_average_not_zero():
    """Production bug: Angel reports avgnetprice/netprice 0 for positions carried overnight; the cost is in cf*avgprice."""
    from tradepulse.brokers import _angel_average
    assert _angel_average({"netqty": "-325", "avgnetprice": "0.00", "netprice": "0.00", "cfsellavgprice": "116.73",
                           "cfbuyavgprice": "0.00"}) == 116.73
    assert _angel_average({"netqty": "390", "avgnetprice": "0", "netprice": "0", "cfbuyavgprice": "50.23"}) == 50.23
    assert _angel_average({"netqty": "75", "avgnetprice": "101.5", "cfbuyavgprice": "99"}) == 101.5
    assert _angel_average({"netqty": "75"}) == 0.0


def test_unknown_cost_uses_broker_pnl_never_avg_zero(rt, brokers, fast_slot):
    """The live -22,347: two carried legs with avg 0. Book P&L must be the broker's own (-3,999.45), not net x LTP."""
    from decimal import Decimal
    rt.store.set_monitoring(["KS50310"], True, "t")
    rt.keeper.apply_snapshot("KS50310", "positions", [
        {"exchange": "NFO", "exchange_token": 64462, "product": "NRML", "net_quantity": -325, "broker_symbol": "NIFTY23NOV2625400CE",
         "average_price": 0.0, "last_price": 137.7, "pnl": -6815.25},
        {"exchange": "NFO", "exchange_token": 64487, "product": "NRML", "net_quantity": 390, "broker_symbol": "NIFTY23NOV2625750CE",
         "average_price": 0.0, "last_price": 57.45, "pnl": 2815.8}], "USER:admin")
    pos = {p["symbol"]: p for p in rt.oms.positions(["KS50310"], "open")}
    assert pos["NIFTY23NOV2625400CE"]["unrealized_pnl_d"] == Decimal("-6815.25")
    assert pos["NIFTY23NOV2625400CE"]["average_entry_price"] is None and pos["NIFTY23NOV2625400CE"]["pnl_source"] == "broker"
    assert rt.oms.pnl(["KS50310"])["total"]["net"] == -3999.45
    # a later trade on a cost-unknown position: no invented realized P&L, open P&L becomes n/a (not avg 0 maths)
    from tradepulse.book import apply_fill
    p = {"net_quantity": -325, "average_price": "0", "realized": "0", "cost_known": 0}
    assert apply_fill(p, "BUY", 100, "140.00") == "REDUCED" and p["realized"] == "0.00" and p["cost_known"] == 0
    assert apply_fill(p, "BUY", 225, "141.00") == "CLOSED" and p["cost_known"] == 1


def test_all_three_brokers_parse(rt, brokers, fast_slot):
    for acct in ("ZM4521", "AM88213", "KS50310"):
        brokers.set(acct, positions=[{"exchange": "NFO", "token": 56723, "product": "NRML", "qty": -75, "symbol": "NIFTY26NOV26000CE", "avg": 100.0}],
                    holdings=[{"exchange": "NSE", "token": 1594, "symbol": "INFY-EQ", "qty": 4, "avg": 1500.0}])
    rt.store.set_monitoring(["ZM4521", "AM88213", "KS50310"], True, "t")
    res = rt.reconciler.run("SCHEDULED 09:00")
    assert res["snapshots"] == 6 and res["errors"] == 0
    for acct in ("ZM4521", "AM88213", "KS50310"):
        p = rt.oms.positions([acct], "open")[0]
        assert (p["exchange"], p["exchange_token"], p["product"], p["net_quantity"]) == ("NFO", 56723, "NRML", -75)
        assert rt.oms.holdings([acct])[0]["quantity"] == 4


def test_oms_position_book_event_rebaselines_without_api(rt, brokers):
    start_live(rt)
    rt.sim.position_book("AM88213", [{"exchange": "NSE", "exchange_token": 1594, "product": "CNC", "net_quantity": 7,
                                      "broker_symbol": "INFY-EQ", "average_price": 1500.0, "last_price": 1510.0, "pnl": 70.0}])
    pump(rt)
    assert brokers.calls == []
    p = rt.oms.positions(["AM88213"], "open")[0]
    assert p["net_quantity"] == 7 and p["baseline_source"] == "OMS_WORKER"


def test_no_broker_calls_from_streams_ticks_or_notifications(rt, brokers):
    start_live(rt)
    for _ in range(5):
        rt.sim.order("ZM4521", outcome="fill")
    pump(rt)
    rt.notifier.run_once()
    rt.scheduler.run_once()
    assert brokers.calls == []


def test_concurrent_full_runs_refused(rt, brokers):
    started, release = threading.Event(), threading.Event()
    rt.store.set_monitoring(["ZM4521"], True, "t")

    def slow_wait(stop=None):
        started.set()
        release.wait(2)
    rt.readers.wait_slot = slow_wait
    t = threading.Thread(target=rt.reconciler.run, args=("SCHEDULED 09:00",))
    t.start()
    started.wait(2)
    assert rt.reconciler.run("USER:x full") == {"status": "ALREADY_RUNNING"}
    release.set()
    t.join(3)


# --- API ---------------------------------------------------------------------------------------------
def test_api_refresh_429_with_retry_after(rt, client, brokers):
    login(client, "mathew", "mathew123")
    ok = client.post("/api/reconcile/request", json={"account_id": "ZM4521", "kind": "positions"}, headers=H)
    assert ok.status_code == 200 and ok.json()["next_allowed_ms"] == 3000
    busy = client.post("/api/reconcile/request", json={"account_id": "AM88213", "kind": "holdings"}, headers=H)
    assert busy.status_code == 429 and int(busy.headers["retry-after"]) in (1, 2, 3)
    assert "Please wait" in busy.json()["detail"]
    other = client.post("/api/reconcile/request", json={"account_id": "J252505", "kind": "positions"}, headers=H)
    assert other.status_code == 404                                             # not Mathew's account


def test_api_holdings_and_reconcile_status_scoped(rt, client, brokers, fast_slot):
    brokers.set("ZM4521", holdings=[{"exchange": "NSE", "token": 2885, "symbol": "RELIANCE-EQ", "qty": 10, "avg": 1300.0}])
    brokers.set("J252505", holdings=[{"exchange": "NSE", "token": 1594, "symbol": "INFY-EQ", "qty": 2, "avg": 1500.0}])
    rt.store.set_monitoring(["ZM4521", "J252505"], True, "t")
    rt.reconciler.run("SCHEDULED 09:00")
    login(client, "mathew", "mathew123")
    h = client.get("/api/holdings").json()
    assert [x["symbol"] for x in h] == ["RELIANCE-EQ"] and h[0]["masked_account"] == "****4521"
    s = client.get("/api/reconcile").json()
    assert {a["account_id"] for a in s["accounts"]} == {"ZM4521", "AM88213"} and s["slot_seconds"] == 3


def test_api_full_run_admin_only(client):
    login(client, "rmanager", "manager123")
    assert client.post("/api/reconcile/run", headers=H).status_code == 403


def test_bot_refresh_wait_message(rt, tg):
    from tradepulse.bot import create_link_code
    u = rt.oms.user_by_username("mathew")
    code = create_link_code(rt.store, u["id"])["code"]
    me = {"id": 2002, "first_name": "M"}
    chat = {"id": 2002, "type": "private"}

    def say(text):
        rt.bot.handle_update({"update_id": 1, "message": {"message_id": 1, "from": me, "chat": chat, "text": text}})
    say(f"/link {code}")
    say("/refresh positions mathew")
    assert "Name exactly one account" in tg.texts()[-1]                         # Mathew has two accounts
    say("/refresh positions 4521")
    assert "refreshed from the broker" in tg.texts()[-1]
    say("/refresh holdings 8213")
    assert "Please wait" in tg.texts()[-1] and "3 seconds" in tg.texts()[-1]
