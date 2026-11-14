"""AI assistant: tool loop with Claude (faked), scope enforcement inside tools, rules-engine fallback."""

import json

from conftest import INSTRUMENTS, FakeClaude, block_text, block_tool, pump, resp, start_live
from tradepulse.assistant import Assistant, Tools


def seed(rt):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="fill", instrument=INSTRUMENTS[0])
    rt.sim.order("J252505", outcome="reject", reject_message="RMS: insufficient margin")
    pump(rt)


def scope_of(rt, username):
    return rt.svc.scope_for_user(rt.oms.user_by_username(username))


def test_claude_tool_loop_and_request_shape(rt):
    seed(rt)
    fake = FakeClaude([
        resp("tool_use", block_text("Checking."), block_tool("t1", "get_positions", {"account": "Mathew", "state": "open"})),
        resp("end_turn", block_text("Mathew has 1 open position: NIFTY26NOV26000CE LONG 75.")),
    ])
    a = Assistant(rt.svc, rt.store, rt.settings, client=fake)
    out = a.answer("Which positions are open for Mathew?", scope_of(rt, "admin"))
    assert out["engine"] == "claude" and "NIFTY26NOV26000CE" in out["answer"] and out["tools"] == ["get_positions"]
    req = fake.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["output_config"] == {"effort": "low"}
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in req and "temperature" not in req
    # second call carries the assistant turn + one tool_result block with the data
    second = fake.requests[1]["messages"]
    tr = second[-1]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "t1"
    data = json.loads(tr["content"])
    assert data["count"] == 1 and data["positions"][0]["account"] == "****4521"
    assert "ZM4521" not in tr["content"]                                    # full account id never reaches the model


def test_tools_cannot_escape_scope(rt):
    seed(rt)
    t = Tools(rt.svc, scope_of(rt, "mathew"))
    out = t.run("get_orders", {"account": "Jayesh"})
    assert "error" in out and all("Jayesh" not in s for s in out["available"])
    orders_all = t.run("get_orders", {})
    assert {o["account"] for o in orders_all["orders"]} == {"****4521"}
    jayesh_order = rt.store.one("SELECT internal_order_id FROM events WHERE account_id = 'J252505'")["internal_order_id"]
    assert "error" in t.run("get_order", {"order_id": jayesh_order})


def test_refusal_is_handled(rt):
    fake = FakeClaude([resp("refusal")])
    out = Assistant(rt.svc, rt.store, rt.settings, client=fake).answer("x", scope_of(rt, "admin"))
    assert out["engine"] == "claude" and "can't help" in out["answer"]


def test_api_error_falls_back_to_rules(rt):
    seed(rt)
    fake = FakeClaude([RuntimeError("boom")])
    out = Assistant(rt.svc, rt.store, rt.settings, client=fake).answer("show rejected orders", scope_of(rt, "admin"))
    assert out["engine"] == "rules" and "REJECTED" in out["answer"] and "insufficient margin" in out["answer"]


def test_rules_engine_intents(rt):
    seed(rt)
    a = Assistant(rt.svc, rt.store, rt.settings)           # AI disabled in test settings
    sc = scope_of(rt, "admin")
    assert a.engine == "rules"
    assert "NIFTY26NOV26000CE" in a.answer("which positions are open?", sc)["answer"]
    assert "Trades today" in a.answer("what trades were executed today?", sc)["answer"]
    assert "P&L" in a.answer("what is my overall pnl", sc)["answer"]
    st = a.answer("what is the current status of Jayesh's account?", sc)["answer"]
    assert "Jayesh Mehta" in st and "Mathew" not in st
    oid = rt.store.one("SELECT internal_order_id FROM events WHERE account_id = 'J252505'")["internal_order_id"]
    assert "Lifecycle" in a.answer(f"what is the status of order {oid}?", sc)["answer"]
    assert "<b>" not in a.answer("status", sc)["answer"]                  # plain text out


def test_assistant_api_scoped(rt, client):
    from conftest import H, login
    seed(rt)
    login(client, "mathew", "mathew123")
    r = client.post("/api/assistant/ask", json={"question": "show rejected orders today"}, headers=H).json()
    assert "No matching orders" in r["answer"]                            # Jayesh's rejection is not Mathew's
    assert rt.store.one("SELECT actor, channel FROM assistant_log") == {"actor": "mathew", "channel": "dashboard"}
