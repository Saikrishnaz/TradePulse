"""Reporting schedules, and structural guarantees: no broker polling, no OMS writes, namespace discipline."""

import re
from datetime import datetime
from pathlib import Path

import pytest

from conftest import setup_group
from tradepulse.bus import MemoryBus, own
from tradepulse.config import IST

PKG = Path(__file__).resolve().parent.parent / "tradepulse"


def at(h, m, day=13):                    # 2026-11-13 is a Friday
    return datetime(2026, 11, day, h, m, tzinfo=IST)


def test_interval_digest_only_in_market_hours(rt):
    gid, _ = setup_group(rt, ["ZM4521"], digest_interval_min=30, eod_summary=0)
    assert [j[1] for j in rt.scheduler.due_jobs(at(8, 0))] == []
    assert rt.scheduler.run_once(at(10, 0)) == 1
    assert rt.scheduler.run_once(at(10, 10)) == 0                      # not yet 30 min
    assert rt.scheduler.run_once(at(10, 31)) == 1
    assert rt.scheduler.run_once(at(10, 0, day=14)) == 0               # Saturday


def test_fixed_times_and_eod_once(rt):
    setup_group(rt, ["ZM4521"], digest_times='["12:00"]', eod_summary=1)
    assert rt.scheduler.run_once(at(11, 59)) == 0
    assert rt.scheduler.run_once(at(12, 1)) == 1
    assert rt.scheduler.run_once(at(12, 5)) == 0
    assert rt.scheduler.run_once(at(15, 41)) == 1                       # EOD at 15:40 default
    assert rt.scheduler.run_once(at(15, 50)) == 0


def test_position_reports_and_queue(rt, tg):
    setup_group(rt, ["ZM4521"], position_interval_min=15, eod_summary=0)
    assert rt.scheduler.run_once(at(10, 0)) == 1
    n = rt.store.one("SELECT kind, text FROM notifications")
    assert n["kind"] == "positions" and "positions" in n["text"].lower()
    rt.notifier.run_once()
    assert len(tg.sent) == 1


def test_group_without_active_chat_gets_nothing(rt):
    gid, ch = setup_group(rt, ["ZM4521"], digest_interval_min=5)
    rt.store.execute("UPDATE channels SET status = 'PENDING' WHERE id = ?", (ch,))
    assert rt.scheduler.run_once(at(10, 0)) == 0


# --- structural guarantees ----------------------------------------------------------------------
BROKER_MARKERS = re.compile(r"kiteconnect|smartapi|SmartConnect|api\.kite\.trade|angelbroking|angelone\.in|"
                            r"5paisa\.com|py5paisa|getOrderBook|order_book\(|tradebook|positions\(\)", re.I)


BROKER_MODULE = PKG / "brokers.py"


@pytest.mark.parametrize("path", [p for p in sorted(PKG.rglob("*.py")) if p != BROKER_MODULE], ids=lambda p: p.name)
def test_no_broker_api_outside_the_readers_module(path):
    """Broker access lives in ONE module (brokers.py) and nowhere else: no SDKs, no broker hosts."""
    text = path.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    assert not BROKER_MARKERS.search(code), f"{path.name} references a broker API"
    assert not re.search(r"kite\.trade|angelone\.in|5paisa\.com", code, re.I), f"{path.name} names a broker host"


def test_broker_module_is_read_only_positions_and_holdings():
    from tradepulse.brokers import ENDPOINTS
    assert set(ENDPOINTS) == {(b, k) for b in ("zerodha", "angel", "5paisa") for k in ("positions", "holdings")}
    paths = " ".join(p for _m, p in ENDPOINTS.values()).lower()
    for forbidden in ("order/v1/placeorder", "modify", "cancel", "/orders", "trades", "tradebook", "margin", "quote", "ltp", "gtt"):
        assert forbidden not in paths
    code = BROKER_MODULE.read_text(encoding="utf-8")
    assert "kiteconnect" not in code.lower() and "smartapi" not in code.replace("SmartAPI REST", "").lower()
    assert code.count("self.http.get(") == 2 and code.count("self.http.post(") == 1       # kite GET, angel GET, 5paisa POST (its API is POST-only)
    assert "retry" not in code.lower().replace("no retries", "")


def test_outbound_http_hosts():
    hosts = set()
    for p in PKG.rglob("*.py"):
        hosts |= set(re.findall(r"https://([a-z0-9.-]+)", p.read_text(encoding="utf-8"), re.I))
    # fonts.* appear only in the Content-Security-Policy header (browser-side)
    assert {h.lower() for h in hosts} <= {"api.telegram.org", "graph.facebook.com", "t.me", "fonts.googleapis.com", "fonts.gstatic.com",
                                          "api.kite.trade", "apiconnect.angelone.in", "openapi.5paisa.com"}, hosts


def test_no_oms_database_or_api_access():
    for p in PKG.rglob("*.py"):
        code = p.read_text(encoding="utf-8")
        if p.name == "users.py":                       # the one-time, read-only import tool
            assert "mode=ro" in code
            continue
        if p.name == "admin.py":                       # its CLI (documents the --db argument)
            continue
        assert "skymatrix.db" not in code and "oms_orders" not in code and ":8890" not in code, p.name


def test_tradepulse_only_writes_its_own_namespace():
    b = MemoryBus()
    for bad in ("smx:oms:events", "smx:session:X", "LTP:NFO:OPT", "userconfig:userdetail"):
        with pytest.raises(RuntimeError):
            own(bad)
        with pytest.raises(RuntimeError):
            b.set(bad, "x")
        with pytest.raises(RuntimeError):
            b.add(bad, {"x": 1})
    own("tradepulse:anything")


def test_oms_stream_read_is_plain_xread(rt):
    """Watching the OMS stream must not create a consumer group (metadata) on the OMS's key."""
    rt.bus.add_foreign("smx:oms:events", {"type": "ORDER_STATUS", "data": "{}"})
    msgs = rt.bus.read("smx:oms:events", "0-0", count=10)
    assert msgs and ("smx:oms:events", "oms-api") not in rt.bus.groups
    assert all(k[0] != "smx:oms:events" for k in rt.bus.groups)


def test_tail_blocks_on_xread_instead_of_polling(rt):
    import threading
    import time
    rt.tail.tick()
    got = {}

    def run():
        t0 = time.monotonic()
        got["n"] = rt.tail.tick(block_ms=3000)
        got["t"] = time.monotonic() - t0
    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.3)
    rt.sim.heartbeat("ZM4521")
    th.join(5)
    assert got["n"] >= 1 and got["t"] < 2.5                               # woke on the message, did not sleep the full 3 s
