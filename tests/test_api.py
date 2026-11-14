"""Dashboard API: TradePulse's own users (imported from the OMS once), CSRF, role scoping, validation, exports."""

from conftest import H, INSTRUMENTS, login, pump, start_live


def test_login_with_tradepulse_users(client):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}, headers=H).status_code == 401
    me = login(client, "admin", "admin123")
    assert me["role"] == "ADMIN" and me["can_configure"] is True
    assert client.get("/api/auth/me").json()["username"] == "admin"


def test_unauthenticated_is_401(client):
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/events/stream").status_code == 401


def test_disabled_user_is_locked_out_immediately(rt, client):
    login(client, "mathew", "mathew123")
    rt.store.execute("UPDATE users SET status = 'DISABLED' WHERE username = 'mathew'")
    assert client.get("/api/overview").status_code == 401


def test_users_crud_admin_only(rt, client):
    login(client, "rmanager", "manager123")
    assert client.get("/api/users").status_code == 403
    client.post("/api/auth/logout", headers=H)
    login(client, "admin", "admin123")
    r = client.post("/api/users", json={"username": "neha", "name": "Neha P", "role": "CLIENT", "password": "short"}, headers=H)
    assert r.status_code == 400
    r = client.post("/api/users", json={"username": "neha", "name": "Neha P", "role": "CLIENT", "password": "longenough1",
                                        "account_ids": ["KS50310"]}, headers=H)
    uid = r.json()["id"]
    assert client.post("/api/users", json={"username": "NEHA", "name": "x", "role": "CLIENT", "password": "longenough1"},
                       headers=H).status_code == 409
    users = {u["username"]: u for u in client.get("/api/users").json()}
    assert [a["account_id"] for a in users["neha"]["accounts"]] == ["KS50310"]
    me = client.get("/api/auth/me").json()
    assert client.put(f"/api/users/{me['id']}", json={"username": "admin", "name": "A", "role": "CLIENT"}, headers=H).status_code == 400
    assert client.put(f"/api/users/{uid}", json={"username": "neha", "name": "Neha P", "role": "CLIENT", "password": "newpassword9"},
                      headers=H).status_code == 200
    client.post("/api/auth/logout", headers=H)
    login(client, "neha", "newpassword9")
    assert {a["account_id"] for a in client.get("/api/monitoring/accounts").json()} == {"KS50310"}


def test_import_users_from_oms_once(rt, tmp_path):
    import demo_oms
    from tradepulse.users import import_oms
    db = demo_oms.create(tmp_path / "oms.db", seed_activity=False)
    out = import_oms(rt.store, db)
    assert out["users_added"] == 0                     # same usernames already exist: never overwritten
    rt.store.execute("DELETE FROM users WHERE username = 'kiran'")
    assert import_oms(rt.store, db)["users_added"] == 1
    k = rt.store.one("SELECT * FROM users WHERE username = 'kiran'")
    assert k["source"] == "OMS_IMPORT" and k["auth_hash"].startswith("$2")


def test_csrf_header_required_for_mutations(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 403


def test_client_sees_only_own_accounts(rt, client):
    start_live(rt)
    rt.sim.order("J252505", outcome="reject")
    rt.sim.order("ZM4521", outcome="reject")
    pump(rt)
    login(client, "mathew", "mathew123")
    ids = {a["account_id"] for a in client.get("/api/monitoring/accounts").json()}
    assert ids == {"ZM4521", "AM88213"}
    assert {e["account_id"] for e in client.get("/api/events").json()} == {"ZM4521"}
    assert {o["account_id"] for o in client.get("/api/orders").json()} == {"ZM4521"}
    assert client.get("/api/accounts/J252505").status_code == 404        # 404, not 403: existence not confirmed
    assert client.get("/api/orders?account_id=J252505").status_code == 404
    jayesh_order = rt.store.one("SELECT internal_order_id FROM events WHERE account_id = 'J252505'")["internal_order_id"]
    assert client.get(f"/api/orders/{jayesh_order}").status_code == 404


def test_manager_scope(client):
    login(client, "rmanager", "manager123")
    names = {c["name"] for c in client.get("/api/clients").json()}
    assert names == {"Mathew", "Jayesh Mehta"}


def test_non_admin_cannot_configure(client):
    login(client, "rmanager", "manager123")
    assert client.post("/api/monitoring/accounts/ZM4521/enable", headers=H).status_code == 403
    assert client.post("/api/groups", json={"name": "x"}, headers=H).status_code == 403
    assert client.put("/api/settings", json={"oms_poll_interval_ms": 500}, headers=H).status_code == 403
    assert client.put("/api/telegram/token", json={"token": ""}, headers=H).status_code == 403


def test_monitoring_select_and_toggle(client):
    login(client, "admin", "admin123")
    r = client.post("/api/monitoring/accounts/select", json={"account_ids": ["ZM4521", "J252505", "BOGUS"], "enabled": True}, headers=H)
    assert r.json()["updated"] == 2
    mon = {a["account_id"]: a["monitored"] for a in client.get("/api/monitoring/accounts").json()}
    assert mon["ZM4521"] and mon["J252505"] and not mon["BR77104"]
    client.post("/api/monitoring/accounts/J252505/disable", headers=H)
    assert not {a["account_id"]: a["monitored"] for a in client.get("/api/monitoring/accounts").json()}["J252505"]


def test_group_crud_and_validation(rt, client):
    login(client, "admin", "admin123")
    ch = rt.store.upsert_channel("telegram", "-100555", title="VIP", enabled=True, status="ACTIVE")
    body = {"name": "VIP Clients", "account_ids": ["ZM4521", "BR77104"], "channel_ids": [ch["id"]],
            "digest_times": ["15:35", "09:30"], "rules": {"ORDER_OPEN": True, "ORDER_PLACED": None}}
    g = client.post("/api/groups", json=body, headers=H).json()
    assert g["account_ids"] == ["BR77104", "ZM4521"] and g["digest_times"] == ["09:30", "15:35"] and g["rules"] == {"ORDER_OPEN": True}
    assert client.post("/api/groups", json=body, headers=H).status_code == 409                       # duplicate name
    assert client.post("/api/groups", json={**body, "name": "B", "account_ids": ["NOPE"]}, headers=H).status_code == 400
    assert client.post("/api/groups", json={**body, "name": "C", "digest_times": ["25:00"]}, headers=H).status_code == 422
    assert client.post("/api/groups", json={**body, "name": "D", "rules": {"BAD": True}}, headers=H).status_code == 400
    upd = client.put(f"/api/groups/{g['id']}", json={**body, "account_ids": ["J252505"], "delivery_mode": "batched"}, headers=H).json()
    assert upd["account_ids"] == ["J252505"] and upd["delivery_mode"] == "batched"
    assert client.delete(f"/api/groups/{g['id']}", headers=H).json()["ok"]
    assert client.get("/api/groups").json() == []
    assert rt.store.rows("SELECT * FROM rules WHERE scope = 'group'") == []


def test_settings_validation(client):
    login(client, "admin", "admin123")
    assert client.put("/api/settings", json={"oms_poll_interval_ms": 10}, headers=H).status_code == 400
    assert client.put("/api/settings", json={"market_open": "9am"}, headers=H).status_code == 400
    assert client.put("/api/settings", json={"notify_modes": ["LIVE", "HACK"]}, headers=H).status_code == 400
    assert client.put("/api/settings", json={"unknown": 1}, headers=H).status_code == 400
    ok = client.put("/api/settings", json={"oms_poll_interval_ms": 500, "notify_modes": ["LIVE"]}, headers=H)
    assert ok.status_code == 200 and ok.json()["values"]["oms_poll_interval_ms"] == 500


def test_rules_endpoint_and_explain(rt, client):
    login(client, "admin", "admin123")
    rt.store.set_monitoring(["ZM4521"], True, "t")
    assert client.put("/api/notifications/settings", json={"scope": "client", "scope_id": "3", "rules": {"ORDER_MODIFIED": False}}, headers=H).status_code == 200
    assert client.put("/api/notifications/settings", json={"scope": "global", "rules": {"NOPE": True}}, headers=H).status_code == 400
    s = client.get("/api/notifications/settings").json()
    assert s["clients"]["3"] == {"ORDER_MODIFIED": False} and s["precedence"][0] == "account"
    ex = client.get("/api/notifications/explain?account_id=ZM4521&event_type=ORDER_FILLED").json()
    assert ex["monitored"] is True and ex["groups"] == []


def test_csv_export_neutralises_formulas(rt, client):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="reject", reject_message="=HYPERLINK(\"http://x\")")
    pump(rt)
    login(client, "admin", "admin123")
    r = client.get("/api/events/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK" in r.text and "\n=HYPERLINK" not in r.text and ",=HYPERLINK" not in r.text
    assert "ZM4521" not in r.text and "****4521" in r.text


def test_event_filters(rt, client):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="reject")
    rt.sim.order("J252505", outcome="cancel")
    pump(rt)
    login(client, "admin", "admin123")
    assert {e["type"] for e in client.get("/api/events?types=ORDER_REJECTED").json()} == {"ORDER_REJECTED"}
    assert {e["broker"] for e in client.get("/api/events?broker=angel").json()} == {"angel"}
    assert {e["client_name"] for e in client.get("/api/events?client=Jayesh").json()} == {"Jayesh Mehta"}
    assert client.get("/api/events?day=2000-01-01").json() == []
    stats = client.get("/api/events/stats").json()
    assert stats["by_type"]["ORDER_REJECTED"] == 1


def test_overview_counts(rt, client):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="fill")
    rt.sim.order("ZM4521", outcome="partial", instrument=INSTRUMENTS[0])
    rt.sim.order("J252505", outcome="reject")
    pump(rt)
    login(client, "admin", "admin123")
    o = client.get("/api/overview").json()
    assert o["orders"]["total"] == 3 and o["orders"]["filled"] == 1 and o["orders"]["partial"] == 1 and o["orders"]["rejected"] == 1
    assert o["trades_today"] == 2 and o["clients"] == 4 and o["accounts"] == 6
    assert "oms_stream" in o["health"]


def test_secrets_never_returned(rt, client):
    login(client, "admin", "admin123")
    client.put("/api/webhooks/generic/secret", json={"secret": "super-secret-value"}, headers=H)
    for url in ("/api/webhooks", "/api/system/health", "/api/telegram", "/api/audit"):
        assert "super-secret-value" not in client.get(url).text


def test_link_code_flow_api(client):
    login(client, "mathew", "mathew123")
    r = client.post("/api/telegram/link-code", headers=H).json()
    assert len(r["code"]) == 14 and r["expires_at"]


def test_health_reads_worker_status_when_workers_run_elsewhere(rt):
    """Production layout: the web process runs no workers; its health must show the worker's published status."""
    import json

    from fastapi.testclient import TestClient

    from tradepulse import bus as busmod
    from tradepulse.app import create_app
    rt.bus.hset(busmod.HEALTH, "redis_tail", json.dumps({"messages": 20005, "streams": 13, "roster_accounts": 13,
                                                          "errors": 0, "redis_ok": True, "last_message_at": "2026-11-14T09:03:30+05:30"}))
    rt.bus.hset(busmod.HEALTH, "telegram_bot", json.dumps({"username": "TradePulseBot", "updates": 4, "replies": 3, "ok": True}))
    rt.bus.hset(busmod.HEALTH, "reconciler", json.dumps({"running": {"trigger": "SCHEDULED 09:00", "done": 3, "total": 10}}))
    rt.settings.RUN_WORKERS = False
    with TestClient(create_app(rt.settings, rt, start_workers=False)) as c:
        login(c, "admin", "admin123")
        h = c.get("/api/system/health").json()
        assert h["redis_tail"]["messages"] == 20005 and h["redis_tail"]["roster_accounts"] == 13
        assert h["telegram"]["username"] == "TradePulseBot"
        assert h["reconcile"]["running"]["done"] == 3
        assert c.get("/api/overview").json()["health"]["oms_stream"] is True
        assert c.get("/api/reconcile").json()["running"]["total"] == 10


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff" and "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "TradePulse" in r.text
