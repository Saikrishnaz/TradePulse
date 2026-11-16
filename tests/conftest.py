import json
import os
import sys
from pathlib import Path

os.environ.setdefault("TP_BCRYPT_ROUNDS", "4")
from urllib.parse import urlparse

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from oms_redis_sim import OmsSim  # noqa: E402
from tradepulse.bus import MemoryBus  # noqa: E402
from tradepulse.config import Settings  # noqa: E402
from tradepulse.runtime import Runtime  # noqa: E402
from tradepulse.users import create_user  # noqa: E402

H = {"X-TradePulse": "1"}

USERS = [  # username, name, role, password, manager
    ("admin", "SkyMatrix Admin", "ADMIN", "admin123", None),
    ("rmanager", "Ravi Manager", "MANAGER", "manager123", None),
    ("mathew", "Mathew", "CLIENT", "mathew123", "rmanager"),
    ("jayesh", "Jayesh Mehta", "CLIENT", "jayesh123", "rmanager"),
    ("bharti", "Bharti Raval", "CLIENT", "bharti123", None),
    ("kiran", "Kiran Shah", "CLIENT", "kiran123", None),
]


class FakeTelegram:
    """Records sendMessage calls; can be told to fail."""

    def __init__(self):
        self.sent = []
        self.edits = []
        self.answers = []
        self.fail = None
        self.updates = []

    def send_message(self, chat_id, text, reply_to=None, reply_markup=None):
        if self.fail is not None:
            raise self.fail
        self.sent.append({"chat_id": str(chat_id), "text": text, "reply_to": reply_to, "markup": reply_markup})
        return {"message_id": len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({"chat_id": str(chat_id), "message_id": message_id, "text": text, "markup": reply_markup})
        return {"message_id": message_id}

    def answer_callback_query(self, callback_id, text="", show_alert=False):
        self.answers.append({"id": callback_id, "text": text, "alert": show_alert})

    def send_chat_action(self, chat_id, action="typing"):
        pass

    def get_me(self):
        return {"username": "TradePulseBot"}

    def set_commands(self, commands):
        pass

    def get_updates(self, offset, timeout=25):
        out, self.updates = self.updates, []
        return out

    def texts(self):
        return [s["text"] for s in self.sent]


class FakeBrokers:
    """The brokers' positions/holdings endpoints. ``books[account] = {"positions": [...], "holdings": [...]}``
    in each broker's own JSON shape is built from simple rows. Every request is recorded."""

    def __init__(self):
        self.books: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail: dict[str, int] = {}          # account -> HTTP status to return

    def set(self, account, positions=None, holdings=None):
        self.books[account] = {"positions": positions or [], "holdings": holdings or []}

    def _account(self, request: httpx.Request) -> str:
        auth = request.headers.get("authorization", "")
        if "TOKEN-" in auth:
            return auth.split("TOKEN-")[-1].strip()
        try:
            return json.loads(request.content or b"{}")["body"]["ClientCode"]
        except Exception:
            return ""

    def handler(self, request: httpx.Request) -> httpx.Response:
        acct = self._account(request)
        path = urlparse(str(request.url)).path
        self.calls.append((acct, request.method, path))
        if acct in self.fail:
            return httpx.Response(self.fail[acct], json={"status": "error", "message": "boom"})
        book = self.books.get(acct, {"positions": [], "holdings": []})
        P, Hh = book["positions"], book["holdings"]
        if path == "/portfolio/positions":
            return httpx.Response(200, json={"status": "success", "data": {"net": [
                {"exchange": p["exchange"], "instrument_token": p["token"] << 8, "product": p["product"], "quantity": p["qty"],
                 "tradingsymbol": p["symbol"], "average_price": p.get("avg", 0), "last_price": p.get("ltp", 0), "pnl": 0} for p in P], "day": []}})
        if path == "/portfolio/holdings":
            return httpx.Response(200, json={"status": "success", "data": [
                {"exchange": h["exchange"], "instrument_token": h["token"] << 8, "tradingsymbol": h["symbol"], "isin": "",
                 "quantity": h["qty"] - h.get("t1", 0), "t1_quantity": h.get("t1", 0), "collateral_quantity": 0, "product": "CNC",
                 "average_price": h.get("avg", 0), "last_price": h.get("ltp", 0), "pnl": 0} for h in Hh]})
        if path.endswith("/getPosition"):
            return httpx.Response(200, json={"status": True, "message": "SUCCESS", "data": [
                {"exchange": p["exchange"], "symboltoken": str(p["token"]), "producttype": {"NRML": "CARRYFORWARD", "MIS": "INTRADAY", "CNC": "DELIVERY"}[p["product"]],
                 "netqty": str(p["qty"]), "tradingsymbol": p["symbol"], "avgnetprice": str(p.get("avg", 0)), "ltp": str(p.get("ltp", 0))} for p in P] or None})
        if path.endswith("/getHolding"):
            return httpx.Response(200, json={"status": True, "message": "SUCCESS", "data": [
                {"exchange": h["exchange"], "symboltoken": str(h["token"]), "tradingsymbol": h["symbol"], "quantity": h["qty"],
                 "t1quantity": h.get("t1", 0), "collateralquantity": 0, "product": "DELIVERY", "averageprice": h.get("avg", 0),
                 "ltp": h.get("ltp", 0)} for h in Hh]})
        if path.endswith("V2/NetPositionNetWise"):
            return httpx.Response(200, json={"head": {"Status": "0"}, "body": {"Status": 0, "NetPositionDetail": [
                {"Exch": p["exchange"][0], "ExchType": "D" if p["exchange"] in ("NFO", "BFO") else "C", "ScripCode": p["token"],
                 "ScripName": p["symbol"], "NetQty": p["qty"], "BuyAvgRate": p.get("avg", 0), "OrderFor": "I" if p["product"] == "MIS" else "D"} for p in P]}})
        if path.endswith("V3/Holding"):
            return httpx.Response(200, json={"head": {"Status": "0"}, "body": {"Status": 0, "Data": [
                {"Exch": h["exchange"][0], "ScripCode": h["token"], "Symbol": h["symbol"], "Quantity": h["qty"], "SellableQty": h["qty"],
                 "AvgRate": h.get("avg", 0)} for h in Hh]}})
        return httpx.Response(404, json={"status": "error", "message": f"unexpected {path}"})

    def broker_calls(self):
        return [c for c in self.calls]


def make_settings(tmp_path, **kw):
    return Settings(TP_DB_PATH=str(tmp_path / "tp.db"), USE_MEMORY_BUS=True, RUN_WORKERS=False,
                    TP_FERNET_KEY="", TP_ADMIN_PASSWORD="", **kw)


def seed_users(store):
    ids = {}
    for username, name, role, pw, _m in USERS:
        ids[username] = create_user(store, username, name, role, pw)
    for username, *_r, mgr in USERS:
        if mgr:
            store.execute("UPDATE users SET manager_id = ? WHERE id = ?", (ids[mgr], ids[username]))
    return ids


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def brokers():
    return FakeBrokers()


@pytest.fixture
def rt(tmp_path, tg, brokers):
    bus = MemoryBus()
    r = Runtime(make_settings(tmp_path), bus=bus, telegram_client=tg,
                broker_http=httpx.Client(transport=httpx.MockTransport(brokers.handler)))
    seed_users(r.store)
    r.sim = OmsSim(bus).seed()
    r.tail.sync_roster()
    yield r
    r.shutdown()


def pump(rt, rounds=3):
    """Read the OMS streams and drain the processor until quiet."""
    n = 0
    for _ in range(rounds):
        rt.tail.tick()
        while True:
            k = rt.processor.run_once(block_ms=0)
            n += k
            if not k:
                break
    return n


def start_live(rt):
    """First tick (history = backfill), then everything after is live."""
    import time
    pump(rt)
    time.sleep(0.005)


def setup_group(rt, accounts, chat_id="-100111", name="Desk", **group_kw):
    rt.store.set_monitoring(accounts, True, "test")
    ch = rt.store.upsert_channel("telegram", chat_id, title=name + " chat", chat_type="supergroup", enabled=True, status="ACTIVE")
    from tradepulse.config import ist_iso
    now = ist_iso()
    cols = {"delivery_mode": "instant", "batch_minutes": 5, "digest_interval_min": 0, "digest_times": "[]",
            "position_interval_min": 0, "eod_summary": 0, "allow_queries": 1}
    cols.update(group_kw)
    gid = rt.store.execute(
        f"INSERT INTO groups (name, created_at, updated_at, {', '.join(cols)}) VALUES (?, ?, ?, {', '.join('?' * len(cols))})",
        (name, now, now, *cols.values()))
    with rt.store.tx() as c:
        c.executemany("INSERT INTO group_accounts (group_id, account_id) VALUES (?, ?)", [(gid, a) for a in accounts])
        c.execute("INSERT INTO group_channels (group_id, channel_id) VALUES (?, ?)", (gid, ch["id"]))
    rt.processor.invalidate()
    return gid, ch["id"]


@pytest.fixture
def client(rt):
    from fastapi.testclient import TestClient

    from tradepulse.app import create_app
    app = create_app(rt.settings, rt, start_workers=False)
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


# Instruments (exchange, token, symbol, class, lot, ref, product) - same as the simulator
from oms_redis_sim import INSTRUMENTS  # noqa: E402,F401


def every_step(rt):
    """Old behaviour: notify every step of an order (the default is one final message per order)."""
    rt.store.put_settings({"order_alerts": "every"})
    rt.processor.invalidate()
