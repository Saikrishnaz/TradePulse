"""The production RedisBus against fakeredis (real stream + consumer-group semantics), end to end."""

import json

import fakeredis
import pytest

from conftest import INSTRUMENTS, make_settings, pump, seed_users, setup_group, start_live
from oms_redis_sim import OmsSim
from tradepulse import bus as busmod
from tradepulse.bus import RedisBus
from tradepulse.runtime import Runtime


@pytest.fixture
def rbus():
    return RedisBus(fakeredis.FakeRedis(decode_responses=True))


def test_stream_group_ack_and_reclaim(rbus):
    rbus.ensure_group(busmod.INGEST, busmod.INGEST_GROUP)
    rbus.ensure_group(busmod.INGEST, busmod.INGEST_GROUP)          # idempotent (BUSYGROUP swallowed)
    rbus.add(busmod.INGEST, {"type": "X", "n": 1})
    got = rbus.read_group(busmod.INGEST, busmod.INGEST_GROUP, "c1", count=10)
    assert [d["n"] for _, d in got] == [1]
    assert rbus.pending_count(busmod.INGEST, busmod.INGEST_GROUP) == 1
    again = rbus.claim_stale(busmod.INGEST, busmod.INGEST_GROUP, "c2", min_idle_ms=0)
    assert [d["n"] for _, d in again] == [1]
    rbus.ack(busmod.INGEST, busmod.INGEST_GROUP, got[0][0])
    assert rbus.pending_count(busmod.INGEST, busmod.INGEST_GROUP) == 0


def test_reads_foreign_oms_stream_without_groups(rbus):
    r = rbus.r
    r.xadd("smx:oms:events", {"type": "FILL", "data": json.dumps({"x": 1})})
    msgs = rbus.read("smx:oms:events", "0-0")
    assert msgs[0][1]["type"] == "FILL"
    assert r.xinfo_groups("smx:oms:events") == []                  # TradePulse created no group on the OMS key
    with pytest.raises(RuntimeError):
        rbus.add("smx:oms:events", {"x": 1})


def test_connects_with_oms_style_credentials(monkeypatch):
    """REDIS_HOST / REDIS_PORT / REDIS_PASSWORD / REDIS_DB - the same names as the OMS .env - and they win over a URL."""
    import redis

    from tradepulse.bus import make_bus
    from tradepulse.config import Settings
    seen = {}

    class Fake:
        def __init__(self, **kw):
            seen.update(kw)

        def ping(self):
            return True

        @classmethod
        def from_url(cls, *a, **kw):
            raise AssertionError("URL must not be used when REDIS_HOST is set")
    monkeypatch.setattr(redis, "Redis", Fake)
    for k, v in {"REDIS_HOST": "10.0.0.5", "REDIS_PORT": "6380", "REDIS_PASSWORD": "s3cret", "REDIS_DB": "2",
                 "TP_REDIS_URL": "redis://other:6379/0", "TP_MEMORY_BUS": "0"}.items():
        monkeypatch.setenv(k, v)
    make_bus(Settings())
    assert (seen["host"], seen["port"], seen["password"], seen["db"]) == ("10.0.0.5", 6380, "s3cret", 2)


def test_ltp_read_only(rbus):
    rbus.r.hset("LTP:NFO:OPT", "56723", json.dumps({"ltp": "131.25"}))
    assert json.loads(rbus.hget_any("LTP:NFO:OPT", "56723"))["ltp"] == "131.25"


def test_full_pipeline_on_redis_with_live_price(tmp_path, tg):
    raw = fakeredis.FakeRedis(decode_responses=True)
    rbus = RedisBus(raw)
    rt = Runtime(make_settings(tmp_path), bus=rbus, telegram_client=tg)
    seed_users(rt.store)
    rt.sim = OmsSim(raw).seed()                      # the 'OMS' writes with its own client, like production
    rt.tail.sync_roster()
    start_live(rt)
    setup_group(rt, ["ZM4521"])
    rt.sim.order("ZM4521", outcome="fill", instrument=INSTRUMENTS[0])
    # market-data publisher's LTP for the option -> unrealised P&L uses it
    rbus.r.hset("LTP:NFO:OPT", "56723", json.dumps({"ltp": "200.00"}))
    pump(rt)
    assert rt.store.one("SELECT COUNT(*) AS n FROM events")["n"] >= 4
    assert rt.notifier.run_once() >= 2 and tg.sent
    pos = rt.oms.positions(["ZM4521"], "open", ltp=rt.svc.ltp)[0]
    assert pos["ltp"] == 200.0
    from decimal import Decimal
    assert pos["unrealized_pnl_d"] == (Decimal(75) * (Decimal("200.00") - Decimal(str(pos["average_entry_price"])))).quantize(Decimal("0.01"))
    assert rbus.length(busmod.LIVE) >= 4
    assert all(k.startswith(("tradepulse:", "LTP:", "smx:")) for k in rbus.r.keys("*"))
    assert [g for s in rbus.r.keys("smx:oms:*") if rbus.r.type(s) == "stream" for g in rbus.r.xinfo_groups(s)] == []


def test_housekeeping_retention(rt):
    from datetime import timedelta

    from tradepulse.config import now_ist
    _gid, ch = setup_group(rt, ["ZM4521"])
    old = (now_ist() - timedelta(days=60)).isoformat(timespec="seconds")
    with rt.store.tx() as c:
        c.execute("""INSERT INTO events (event_id, dedupe_key, type, source, account_id, occurred_at, received_at)
                     VALUES ('old1', 'k-old1', 'ORDER_PLACED', 'T', 'ZM4521', ?, ?)""", (old, old))
        c.execute("""INSERT INTO events (event_id, dedupe_key, type, source, account_id, occurred_at, received_at)
                     VALUES ('old2', 'k-old2', 'ORDER_PLACED', 'T', 'ZM4521', ?, ?)""", (old, old))
        c.execute("""INSERT INTO notifications (event_id, channel_id, text, status, next_attempt_at, created_at)
                     VALUES ('old2', ?, 't', 'DEAD', ?, ?)""", (ch, old, old))
    out = rt.scheduler.housekeeping()
    assert out["events"] == 1                                      # old1 pruned
    assert rt.store.one("SELECT 1 AS x FROM events WHERE event_id = 'old2'")    # still referenced by an undelivered notification
