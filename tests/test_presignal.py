"""Next-day signal intake on TradePulse: auth with the OMS-published keys, validation against the
OMS-published templates and instruments, the shared Redis store, and drift against the OMS copies."""

import hashlib
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

import fakeredis
import pytest
from cryptography.fernet import Fernet

from tradepulse.presignal import contract
from tradepulse.presignal import keys as rk
from tradepulse.presignal.intake import Intake, sign
from tradepulse.presignal.store import PresignalStore, intact

IST = contract.IST
EVENING = datetime(2026, 9, 15, 16, 0, tzinfo=IST)
NEXT = "2026-09-16"
PRESIG_KEY = Fernet.generate_key().decode()
SECRET = "s3cret-for-tests"
OMS_REPO = Path(os.getenv("SMX_OMS_REPO", Path(__file__).resolve().parents[2] / "GITHUBS" / "SkyMatrix_Dashboard"))


def _dump(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


@pytest.fixture
def r():
    """Redis as the OMS publisher leaves it (backend/oms/presignal/publish.py)."""
    c = fakeredis.FakeRedis(decode_responses=True)
    legs = [{"leg_id": "LEG_1", "option_type": "CE", "enabled": True, "exchange": "", "underlying": ""},
            {"leg_id": "LEG_2", "option_type": "PE", "enabled": True, "exchange": "", "underlying": ""}]
    c.set(rk.presig_strategy("STRAT_001"), _dump({
        "strategy": {"strategy_id": "STRAT_001", "enabled": 1, "category": "OPTIONS", "underlying": "NIFTY"},
        "config": {"version": 3, "params": {}, "legs": legs},
        "settings": {"allow_market_orders": False, "price_tick_policy": "REJECT"}}))
    c.set(rk.presig_strategy("BSK"), _dump({
        "strategy": {"strategy_id": "BSK", "enabled": 1, "category": "EQUITY", "underlying": ""},
        "config": {"version": 1, "params": {"strategy_mode": "BASKET", "basket": {"defaults": {"EQ": {"product": "CNC"}}}},
                   "legs": [{"leg_id": "EQ", "instrument_type": "EQUITY", "enabled": True}]},
        "settings": {"allow_market_orders": False, "price_tick_policy": "REJECT"}}))
    c.set(rk.presig_strategy("OFF"), _dump({
        "strategy": {"strategy_id": "OFF", "enabled": 0, "category": "OPTIONS"}, "config": None, "settings": {}}))
    c.set(rk.PRESIG_CALENDAR, _dump(contract.Calendar().to_dict()))
    cipher = Fernet(PRESIG_KEY.encode())
    c.set(rk.PRESIG_ALGO_KEYS, _dump({
        "algo_one": {"auth_mode": "BEARER_HMAC", "enabled": True, "allowed_strategies": ["*"],
                     "secret_enc": cipher.encrypt(SECRET.encode()).decode()},
        "algo_off": {"auth_mode": "BEARER_HMAC", "enabled": False, "allowed_strategies": ["*"],
                     "secret_enc": cipher.encrypt(SECRET.encode()).decode()},
        "algo_narrow": {"auth_mode": "HMAC", "enabled": True, "allowed_strategies": ["OTHER"],
                        "secret_enc": cipher.encrypt(SECRET.encode()).decode()}}))
    c.set(rk.PRESIG_INSTRUMENTS_CURRENT, "v1")
    c.hset(rk.presig_instruments("v1"), mapping={
        "NFO:47317": _dump(["CE", 65, 0.05, "2026-09-22", "NIFTY", "NIFTY26922CE", "NIFTY22SEP26CE"]),
        "NFO:47318": _dump(["PE", 65, 0.05, "2026-09-22", "NIFTY", "NIFTY26922PE", "NIFTY22SEP26PE"]),
        "NFO:69824": _dump(["CE", 30, 0.05, "2026-09-22", "BANKNIFTY", "BANKNIFTY26922CE", "BANKNIFTY22SEP26CE"]),
        "NSE:2885": _dump(["EQ", 1, 0.05, "", "RELIANCE INDUSTRIES", "RELIANCE", "RELIANCE-EQ"]),
    })
    return c


@pytest.fixture
def intake(r):
    clock = {"now": EVENING}
    it = Intake(lambda: r, PRESIG_KEY, clock=lambda: clock["now"])
    it.clock_ref = clock
    return it


def payload(signal_id=None, version=1, policy="PREMARKET", orders=None, strategy_id="STRAT_001", trading_date=NEXT):
    orders = orders if orders is not None else [
        {"leg_id": "LEG_1", "tag": "T1", "side": "BUY", "exchange": "NFO", "exchange_token": 47317,
         "quantity_percent": 100, "product": "NRML", "order_type": "LIMIT", "price": 125.5},
        {"leg_id": "LEG_2", "tag": "T1", "side": "SHORT", "exchange": "NFO", "exchange_token": 47318,
         "quantity_percent": 100, "product": "NRML", "order_type": "LIMIT", "price": 80.05}]
    return {"signal_id": signal_id or f"SIG_{secrets.token_hex(5)}", "strategy_id": strategy_id,
            "trading_date": trading_date, "version": version, "execution": {"policy": policy}, "orders": orders}


def headers(body: bytes, key_id="algo_one", secret=SECRET, nonce=None, ts=None):
    ts = str(int(time.time()) if ts is None else ts)
    nonce = nonce or secrets.token_hex(12)
    return {"Authorization": f"Bearer {key_id}.{secret}", "X-SMX-Key-Id": key_id, "X-SMX-Timestamp": ts,
            "X-SMX-Nonce": nonce, "X-SMX-Signature": sign(secret, ts, nonce, body)}


def send(intake, p, **kw):
    body = json.dumps(p).encode()
    return intake.handle(body, headers(body, **kw), "10.0.0.5")


# ------------------------------------------------------------------ store + auth
def test_valid_signal_is_stored_for_the_oms(intake, r):
    p = payload("TP_SIG_001")
    status, body = send(intake, p)
    assert status == 202 and body["status"] == "STORED" and body["trading_date"] == NEXT
    snap = PresignalStore(r).get(NEXT, "STRAT_001", "TP_SIG_001")
    assert intact(snap) and snap["source"] == "TRADEPULSE" and snap["key_id"] == "algo_one"
    assert snap["config_version"] == 3 and snap["tags"] == ["T1"] and snap["payload"] == p
    # Writes outside smx:presig: are impossible through this path.
    assert all(k.startswith(("smx:presig:",)) for k in r.keys("*"))


def test_resend_versions_and_stale(intake, r):
    p = payload("TP_SIG_002")
    send(intake, p)
    assert send(intake, p)[1]["status"] == "DUPLICATE"
    v2 = payload("TP_SIG_002", version=2)
    v2["orders"][0]["price"] = 130
    assert send(intake, v2)[0] == 202
    status, body = send(intake, payload("TP_SIG_002", version=2))
    assert status == 409 and body["code"] == "STALE_VERSION"
    assert [s["version"] for s in PresignalStore(r).versions(NEXT, "STRAT_001", "TP_SIG_002")] == [1, 2]


@pytest.mark.parametrize("kw, code", [
    ({"secret": "wrong"}, "INVALID_CREDENTIALS"),
    ({"key_id": "algo_off"}, "INVALID_CREDENTIALS"),
    ({"key_id": "nobody"}, "INVALID_CREDENTIALS"),
    ({"ts": 1000}, "TIMESTAMP_OUT_OF_WINDOW"),
])
def test_auth_refusals(intake, r, kw, code):
    status, body = send(intake, payload(), **kw)
    assert status == 401 and body["error"] == code
    assert PresignalStore(r).pending(NEXT) == ([], [])


def test_wrong_presig_key_refuses_everything(r):
    it = Intake(lambda: r, Fernet.generate_key().decode(), clock=lambda: EVENING)
    status, body = send(it, payload())
    assert status == 401 and body["error"] == "INVALID_CREDENTIALS"


def test_nonce_is_shared_with_the_oms(intake, r):
    body = json.dumps(payload("TP_SIG_003")).encode()
    h = headers(body, nonce="sharednonce123")
    r.set(rk.presig_nonce("algo_one", "sharednonce123"), "OMS")      # the OMS saw this request first
    status, out = intake.handle(body, h)
    assert status == 401 and out["error"] == "REPLAYED_NONCE"


def test_key_strategy_list(intake):
    status, body = send(intake, payload(), key_id="algo_narrow")
    assert status == 403 and body["code"] == "STRATEGY_NOT_ALLOWED"


def test_redis_missing_stores_nothing():
    it = Intake(lambda: None, PRESIG_KEY, clock=lambda: EVENING)
    status, body = send(it, payload())
    assert status == 503 and body["code"] == "STORE_UNAVAILABLE"


# ------------------------------------------------------------------ validation
def _order(**kw):
    o = {"leg_id": "LEG_1", "tag": "T9", "side": "BUY", "exchange": "NFO", "exchange_token": 47317,
         "quantity_percent": 100, "product": "NRML", "order_type": "LIMIT", "price": 100}
    o.update(kw)
    return o


@pytest.mark.parametrize("order, code", [
    (_order(side="SELL"), "NEXT_DAY_EXIT_NOT_SUPPORTED"),
    (_order(price=None), "PREMARKET_NEEDS_LIMIT_PRICE"),
    (_order(exchange_token=99999), "INSTRUMENT_NOT_FOUND"),
    (_order(exchange_token=47318), "OPTION_TYPE_MISMATCH"),
    (_order(exchange_token=69824), "UNDERLYING_MISMATCH"),
    (_order(leg_id="LEG_9"), "INVALID_LEG_ID"),
    (_order(price=100.03), "PRICE_NOT_ON_TICK"),
    (_order(product="CNC"), "INVALID_PRODUCT"),
    (_order(quantity_percent=0), "INVALID_QUANTITY_PERCENT"),
    (_order(order_type="MARKET", price=None), "MARKET_ORDERS_DISABLED"),
    (_order(tag=""), "INVALID_TAG"),
])
def test_order_refusals(intake, order, code):
    status, body = send(intake, payload(orders=[order]))
    assert status == 422 and body["errors"][0]["code"] == code, body


def test_unpriced_entry_is_fine_for_policy_open(intake):
    assert send(intake, payload(policy="OPEN", orders=[_order(price=None)]))[0] == 202


def test_expiry_against_the_trading_date(intake):
    intake.clock_ref["now"] = datetime(2026, 9, 22, 16, 0, tzinfo=IST)
    status, body = send(intake, payload(trading_date="2026-09-23"))
    assert status == 422 and body["errors"][0]["code"] == "INSTRUMENT_EXPIRED"


def test_strategy_refusals(intake):
    assert send(intake, payload(strategy_id="NOPE"))[1]["code"] == "STRATEGY_NOT_FOUND"
    assert send(intake, payload(strategy_id="OFF"))[1]["code"] == "STRATEGY_DISABLED"


def test_window_and_session(intake):
    intake.clock_ref["now"] = datetime(2026, 9, 16, 11, 0, tzinfo=IST)
    assert send(intake, payload(trading_date="2026-09-16"))[1]["code"] == "INTAKE_CLOSED"
    intake.clock_ref["now"] = EVENING
    assert send(intake, payload(trading_date="2026-09-18"))[1]["code"] == "INVALID_TRADING_DATE"


def test_basket_strategy_uses_template_defaults(intake):
    o = {"tag": "D1", "side": "BUY", "exchange": "NSE", "exchange_token": 2885, "quantity": 10, "price": 1420}
    assert send(intake, payload(strategy_id="BSK", orders=[o]))[0] == 202
    o2 = {**o, "tag": "D2", "exchange": "NFO", "exchange_token": 47317}
    status, body = send(intake, payload(strategy_id="BSK", orders=[o2]))
    assert body["errors"][0]["code"] == "SEGMENT_NOT_ALLOWED"


def test_tag_and_lock_rules_shared_with_the_oms(intake, r):
    send(intake, payload("TP_SIG_010"))
    assert send(intake, payload("TP_SIG_011"))[1]["code"] == "TAG_ALREADY_STORED"
    PresignalStore(r).lock(NEXT, "OMS pickup")
    assert send(intake, payload("TP_SIG_012", orders=[_order(tag="NEW")]))[1]["code"] == "SIGNAL_LOCKED"


# ------------------------------------------------------------------ HTTP
def test_http_route_needs_no_dashboard_header(client, rt, r):
    rt_app = client.app
    rt_app.state.presignal = Intake(lambda: r, PRESIG_KEY, clock=lambda: EVENING)
    body = json.dumps(payload("TP_SIG_HTTP")).encode()
    res = client.post("/api/signals/next-day", content=body, headers=headers(body))
    assert res.status_code == 202, res.text


# ------------------------------------------------------------------ drift
@pytest.mark.parametrize("name", ["contract.py", "store.py", "keys.py"])
def test_shared_files_match_the_oms(name):
    oms = OMS_REPO / "backend" / "oms" / "presignal" / name
    if not oms.exists():
        pytest.skip(f"OMS repo not found at {OMS_REPO} (set SMX_OMS_REPO)")
    here = Path(__file__).resolve().parents[1] / "tradepulse" / "presignal" / name
    def norm(p):                       # git may check either repo out with CRLF on Windows
        return p.read_bytes().replace(b"\r\n", b"\n")

    assert hashlib.sha256(norm(here)).hexdigest() == hashlib.sha256(norm(oms)).hexdigest(), \
        f"tradepulse/presignal/{name} differs from the OMS copy: copy it over unchanged"
