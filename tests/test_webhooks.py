"""Broker webhooks: authenticity, normalization, account resolution, fast ack, dedupe."""

import hashlib
import hmac
import json

from conftest import pump
from tradepulse import bus as busmod


def kite_body(order_id="250101000123", status="COMPLETE", secret="kite-secret", user_id="ZM4521", **kw):
    d = {"user_id": user_id, "order_id": order_id, "order_timestamp": "2026-11-13 10:32:15", "status": status,
         "tradingsymbol": "NIFTY26NOV26000CE", "exchange": "NFO", "transaction_type": "BUY", "order_type": "LIMIT",
         "product": "NRML", "quantity": 75, "filled_quantity": 75, "pending_quantity": 0, "price": 125.5,
         "average_price": 125.5, "exchange_update_timestamp": "2026-11-13 10:32:15", **kw}
    d["checksum"] = hashlib.sha256(f"{d['order_id']}{d['order_timestamp']}{secret}".encode()).hexdigest()
    return d


def test_zerodha_valid_checksum_accepted_and_normalized(rt, client):
    rt.vault.put("webhook:zerodha", "kite-secret")
    r = client.post("/api/webhooks/brokers/zerodha", content=json.dumps(kite_body()))
    assert r.status_code == 200 and r.json()["accepted"] == 1
    pump(rt)
    e = rt.store.one("SELECT * FROM events")
    assert e["type"] == "ORDER_FILLED" and e["account_id"] == "ZM4521" and e["client_name"] == "Mathew"
    assert e["source"] == "WEBHOOK:zerodha" and e["occurred_at"].startswith("2026-11-13T10:32:15")
    log = rt.store.one("SELECT * FROM webhook_log")
    assert log["outcome"] == "ACCEPTED"


def test_zerodha_bad_checksum_rejected_and_logged(rt, client):
    rt.vault.put("webhook:zerodha", "kite-secret")
    r = client.post("/api/webhooks/brokers/zerodha", content=json.dumps(kite_body(secret="wrong")))
    assert r.status_code == 401
    assert rt.bus.length(busmod.INGEST) == 0
    assert rt.store.one("SELECT outcome FROM webhook_log")["outcome"] == "REJECTED"


def test_zerodha_without_configured_secret_is_rejected(client):
    r = client.post("/api/webhooks/brokers/zerodha", content=json.dumps(kite_body()))
    assert r.status_code == 401 and "secret" in r.json()["detail"]


def test_zerodha_update_partial_and_modify(rt, client):
    rt.vault.put("webhook:zerodha", "kite-secret")
    client.post("/api/webhooks/brokers/zerodha", content=json.dumps(kite_body(status="UPDATE", filled_quantity=25, pending_quantity=50)))
    client.post("/api/webhooks/brokers/zerodha", content=json.dumps(kite_body(status="UPDATE", filled_quantity=0, pending_quantity=75, price=126)))
    pump(rt)
    assert [r["type"] for r in rt.store.rows("SELECT type FROM events ORDER BY id")] == ["ORDER_PARTIALLY_FILLED", "ORDER_MODIFIED"]


def test_duplicate_webhook_delivery_is_one_event(rt, client):
    rt.vault.put("webhook:zerodha", "kite-secret")
    body = json.dumps(kite_body())
    for _ in range(3):
        assert client.post("/api/webhooks/brokers/zerodha", content=body).status_code == 200
    pump(rt)
    assert len(rt.store.rows("SELECT * FROM events")) == 1


def test_angel_token_auth_and_order_lookup(rt, client):
    rt.vault.put("webhook:angel", "angel-token")
    body = {"orderid": "999", "orderstatus": "rejected", "tradingsymbol": "INFY-EQ", "exchange": "NSE",
            "transactiontype": "BUY", "quantity": "1", "text": "RMS:Margin Exceeds", "clientcode": "J252505"}
    assert client.post("/api/webhooks/brokers/angel?token=nope", json=body).status_code == 401
    r = client.post("/api/webhooks/brokers/angel?token=angel-token", json=body)
    assert r.status_code == 200
    pump(rt)
    e = rt.store.one("SELECT * FROM events")
    assert e["type"] == "ORDER_REJECTED" and e["account_id"] == "J252505" and e["message"] == "RMS:Margin Exceeds"


def test_unknown_account_is_404_not_guessed(rt, client):
    rt.vault.put("webhook:angel", "t")
    r = client.post("/api/webhooks/brokers/angel?token=t", json={"orderid": "1", "orderstatus": "complete", "clientcode": "NOPE"})
    assert r.status_code == 404


def test_5paisa_reports_limitation(client):
    r = client.post("/api/webhooks/brokers/5paisa", json={})
    assert r.status_code == 501 and "OMS event stream" in r.json()["detail"]


def test_generic_hmac(rt, client):
    rt.vault.put("webhook:generic", "s3cret")
    body = json.dumps({"event_type": "TRADE_EXECUTED", "account_id": "BR77104", "event_id": "x-1", "symbol": "RELIANCE-EQ",
                       "side": "buy", "quantity": 5, "price": 1312.4}).encode()
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert client.post("/api/webhooks/brokers/generic", content=body, headers={"X-TradePulse-Signature": "bad"}).status_code == 401
    assert client.post("/api/webhooks/brokers/generic", content=body, headers={"X-TradePulse-Signature": sig}).status_code == 200
    pump(rt)
    assert rt.store.one("SELECT type, side FROM events") == {"type": "TRADE_EXECUTED", "side": "BUY"}


def test_webhook_needs_no_csrf_header_but_is_size_capped(client):
    r = client.post("/api/webhooks/brokers/generic", content=b"x" * 70000)
    assert r.status_code == 413


def test_get_probe_and_unknown_broker(client):
    assert client.get("/api/webhooks/brokers/zerodha").json()["ok"] is True
    assert client.post("/api/webhooks/brokers/nobroker", json={}).status_code == 404
