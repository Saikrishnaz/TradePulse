"""Interactive Telegram UI: screens, button navigation (edit in place), scope re-checks, refresh pop-ups, alert buttons."""

from conftest import INSTRUMENTS, pump, setup_group, start_live
from tradepulse import tgui
from tradepulse.bot import create_link_code

ME = {"id": 2002, "first_name": "Mathew"}
DM = {"id": 2002, "type": "private"}
GROUP = {"id": -100777, "type": "supergroup", "title": "Desk"}


def send(rt, text, chat=DM, frm=ME):
    rt.bot.handle_update({"update_id": 1, "message": {"message_id": 10, "from": frm, "chat": chat, "text": text}})


def tap(rt, data, chat=DM, frm=ME, message_id=10):
    rt.bot.handle_update({"update_id": 2, "callback_query": {"id": "cb1", "from": frm, "data": data,
                                                             "message": {"message_id": message_id, "chat": chat}}})


def linked(rt, username="mathew"):
    code = create_link_code(rt.store, rt.oms.user_by_username(username)["id"])["code"]
    send(rt, f"/link {code}")


def buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


def test_menu_shows_home_card_with_button_grid(rt, tg):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="fill", instrument=INSTRUMENTS[0])
    pump(rt)
    linked(rt)
    send(rt, "/menu")
    last = tg.sent[-1]
    assert "⚡ <b>TradePulse</b> · your desk" in last["text"] and "Orders" in last["text"] and "Mathew" in last["text"]
    labels = [b["text"] for b in buttons(last["markup"])]
    assert labels == ["📊 Positions", "🧾 Orders", "👤 Accounts", "💰 P&L", "⋯ More", "↻ Update"]   # few buttons
    tap(rt, tgui.cb("more"))
    more = [b["text"] for b in buttons(tg.edits[-1]["markup"])]
    for want in ("💹 Trades", "🏦 Holdings", "📡 Status", "🕒 Recent", "🔄 Refresh from broker", "⬅️ Back"):
        assert want in more
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons(last["markup"]))


def test_tap_edits_the_same_message_in_place(rt, tg):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="fill", instrument=INSTRUMENTS[0])
    pump(rt)
    linked(rt)
    sent_before = len(tg.sent)
    tap(rt, tgui.cb("pos"))
    assert len(tg.sent) == sent_before                       # no new chat message
    e = tg.edits[-1]
    assert e["message_id"] == 10 and "Open positions" in e["text"] and "NIFTY 26000 CE" in e["text"]
    assert "👤 <b>Mathew</b>  <code>ZM4521</code>" in e["text"] and "<blockquote>" in e["text"]   # one block per account
    assert tg.answers[-1]["alert"] is False                  # spinner stopped
    tap(rt, tgui.cb("home"))
    assert "⚡ <b>TradePulse</b>" in tg.edits[-1]["text"]


def test_orders_chips_and_order_drilldown(rt, tg):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="reject", reject_message="RMS: insufficient margin")
    rt.sim.order("ZM4521", outcome="fill", instrument=INSTRUMENTS[0])
    pump(rt)
    linked(rt)
    tap(rt, tgui.cb("ord", arg="rej"))
    e = tg.edits[-1]
    assert "❌" in e["text"] and "insufficient margin" in e["text"]
    labels = [b["text"] for b in buttons(e["markup"])]
    assert "Show: ❌ Rejected ▸" in labels and len(buttons(e["markup"])) <= 5        # one filter button, one footer row
    assert "/order " in e["text"]                                                    # tap-to-copy order command
    oid = rt.store.one("SELECT internal_order_id FROM orders WHERE status = 'REJECTED'")["internal_order_id"]
    tap(rt, tgui.cb("od", arg=oid))
    d = tg.edits[-1]["text"]
    assert "Order rejected" in d or "Rejected" in d
    assert "<b>Lifecycle</b>" in d and "<blockquote expandable>" in d


def test_forged_button_cannot_escape_scope(rt, tg):
    start_live(rt)
    rt.sim.order("J252505", outcome="fill", instrument=INSTRUMENTS[2])        # Jayesh - not Mathew's
    pump(rt)
    linked(rt)
    tap(rt, tgui.cb("pos", acct="J252505"))                                  # hand-crafted button for another account
    assert "BANKNIFTY" not in tg.edits[-1]["text"]
    oid = rt.store.one("SELECT internal_order_id FROM orders WHERE account_id = 'J252505'")["internal_order_id"]
    tap(rt, tgui.cb("od", arg=oid))
    assert "Order not found" in tg.edits[-1]["text"]
    tap(rt, "garbage|data")
    assert "expired" in tg.answers[-1]["text"]


def test_unlinked_tap_gets_alert_no_data(rt, tg):
    tap(rt, tgui.cb("pos"), frm={"id": 999, "first_name": "X"}, chat={"id": 999, "type": "private"})
    assert tg.answers[-1]["alert"] is True and "isn't linked" in tg.answers[-1]["text"]
    assert tg.edits == []


def test_refresh_flow_and_please_wait_popup(rt, tg, brokers):
    linked(rt)
    brokers.set("ZM4521", positions=[{"exchange": "NFO", "token": 56723, "product": "NRML", "qty": 75, "symbol": "NIFTY26NOV26000CE", "avg": 120.0}])
    tap(rt, tgui.cb("ref"))
    assert "Choose an account" in tg.edits[-1]["text"]
    tap(rt, tgui.cb("ref", acct="ZM4521"))
    assert "Refresh from broker" in tg.edits[-1]["text"]
    tap(rt, tgui.cb("rk", acct="ZM4521", arg="positions"))
    assert "Positions refreshed" in tg.edits[-1]["text"] and "NIFTY 26000 CE" in tg.edits[-1]["text"]
    assert len(brokers.calls) == 1
    edits = len(tg.edits)
    tap(rt, tgui.cb("rk", acct="AM88213", arg="holdings"))                   # within 3 s
    a = tg.answers[-1]
    assert a["alert"] is True and "Please wait" in a["text"]
    assert len(brokers.calls) == 1 and len(tg.edits) == edits                 # no broker call, message unchanged


def test_group_screens_scoped_to_group(rt, tg):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="fill", instrument=INSTRUMENTS[0])
    rt.sim.order("BR77104", outcome="fill", instrument=INSTRUMENTS[2])
    pump(rt)
    setup_group(rt, ["ZM4521"], chat_id=str(GROUP["id"]))
    send(rt, "/positions", chat=GROUP, frm={"id": 5, "first_name": "Anyone"})
    t = tg.sent[-1]["text"]
    assert "NIFTY 26000 CE" in t and "BANKNIFTY" not in t


def test_group_desk_is_its_own_accounts_and_modes(rt, tg):
    """A LIVE-only group: its home lists only its accounts, and paper orders/trades are invisible everywhere."""
    start_live(rt)
    rt.sim.order("ZM4521", outcome="fill", instrument=INSTRUMENTS[0])               # LIVE, in the group
    rt.sim.accounts["J252505"]["mode"] = "PAPER"
    rt.sim.order("J252505", outcome="fill", instrument=INSTRUMENTS[2])              # PAPER, in the group
    rt.sim.order("BR77104", outcome="fill", instrument=INSTRUMENTS[1])              # LIVE, not in the group
    pump(rt)
    gid, _ = setup_group(rt, ["ZM4521", "J252505"], chat_id=str(GROUP["id"]), name="Live desk")
    rt.store.execute("UPDATE groups SET modes = ? WHERE id = ?", ('["LIVE"]', gid))
    anyone = {"id": 5, "first_name": "Anyone"}
    send(rt, "/menu", chat=GROUP, frm=anyone)
    home = tg.sent[-1]
    t = home["text"]
    assert "👥 <b>Live desk</b>" in t and "LIVE" in t and "your desk" not in t
    assert "Mathew" in t and "Jayesh" in t and "Bharti" not in t
    assert "1 order " in t and "1 trade " in t                                       # the paper fill is not counted
    labels = [b["text"] for b in buttons(home["markup"])]
    assert any(l.startswith("👤 Mathew") for l in labels) and any(l.startswith("👤 Jayesh") for l in labels)
    assert "📡 Status" not in labels and "🏦 Holdings" not in labels                # the group desk is not the private one
    for view, arg in (("ord", "all"), ("trd", "-"), ("rec", "-")):
        tap(rt, tgui.cb(view, "-", arg), chat=GROUP, frm=anyone)
        assert "BANKNIFTY" not in tg.edits[-1]["text"], view                           # J252505's paper order
    tap(rt, tgui.cb("ac", "J252505"), chat=GROUP, frm=anyone)
    card = tg.edits[-1]["text"]
    assert "Jayesh" in card and "Orders <b>0</b>" in card and "Trades <b>0</b>" in card
    tap(rt, tgui.cb("ac", "BR77104"), chat=GROUP, frm=anyone)                        # forged: not this group's account
    assert "Bharti" not in tg.edits[-1]["text"]


def test_private_desk_shows_every_mode_with_a_tag(rt, tg):
    start_live(rt)
    rt.sim.accounts["ZM4521"]["mode"] = "PAPER"
    rt.sim.order("ZM4521", outcome="fill", instrument=INSTRUMENTS[0])
    pump(rt)
    linked(rt)
    tap(rt, tgui.cb("ord", "-", "all"))
    assert "🧪PAPER" in tg.edits[-1]["text"]
    tap(rt, tgui.cb("ac", "ZM4521"))
    assert "Orders <b>1</b>" in tg.edits[-1]["text"]


def test_alert_cards_carry_buttons(rt, tg):
    start_live(rt)
    setup_group(rt, ["J252505"])
    rt.sim.order("J252505", outcome="reject")
    pump(rt)
    rt.notifier.run_once()
    rejected = next(s for s in tg.sent if "Order rejected" in s["text"])
    labels = [b["text"] for b in buttons(rejected["markup"])]
    assert labels == ["🔍 Order details", "👤 Account"]
    grp, who = {"id": -100111, "type": "supergroup", "title": "Desk chat"}, {"id": 7, "first_name": "M"}
    od = buttons(rejected["markup"])[0]["callback_data"]
    tap(rt, od, chat=grp, frm=who)
    assert "Lifecycle" in tg.edits[-1]["text"]
    back = [b for b in buttons(tg.edits[-1]["markup"])]
    assert [b["text"] for b in back] == ["⬅️ Back to update"]                      # the update is never lost
    tap(rt, back[0]["callback_data"], chat=grp, frm=who)
    assert tg.edits[-1]["text"] == rejected["text"]                                 # original card restored
    tap(rt, buttons(rejected["markup"])[1]["callback_data"], chat=grp, frm=who)
    assert "Jayesh" in tg.edits[-1]["text"] and [b["text"] for b in buttons(tg.edits[-1]["markup"])] == ["⬅️ Back to update"]


def test_every_screen_renders_for_a_busy_account(rt, tg):
    start_live(rt)
    for outcome in ("fill", "partial", "reject", "cancel", "fill", "fill", "fill", "fill"):
        rt.sim.order("ZM4521", outcome=outcome)
    pump(rt)
    scope = rt.svc.scope_for_user(rt.oms.user_by_username("admin"))
    for view, arg in (("home", None), ("pos", None), ("hold", None), ("ord", "all"), ("ord", "work"), ("trd", None),
                      ("pnl", "week"), ("sts", None), ("rec", None), ("ref", None), ("acc", "pos.")):
        text, markup = tgui.render(rt.svc, scope, view, None, arg)
        assert text and len(text) < 4096 and markup["inline_keyboard"], view
        assert all(len(b["callback_data"].encode()) <= 64 for b in buttons(markup)), view
    text, markup = tgui.render(rt.svc, scope, "ord", "ZM4521", "all", 1)          # page 2 exists
    assert any(b["text"] == "▶" or b["text"] == "◀" for b in buttons(markup))
    for view, arg in (("pos", None), ("ord", "all"), ("trd", None), ("pnl", "week"), ("rec", None)):
        _t, markup = tgui.render(rt.svc, scope, view, None, arg)
        assert len(markup["inline_keyboard"]) <= 2, view                              # every list: ≤ 2 rows of buttons


def test_pretty_symbols():
    cases = {"NIFTY23NOV2625400CE": "NIFTY 25400 CE · 23 Nov", "NIFTY26NOV26000CE": "NIFTY 26000 CE · Nov",
             "NIFTY26NOV25800PE": "NIFTY 25800 PE · Nov", "NIFTY2611726000CE": "NIFTY 26000 CE · 17 Jan",
             "BANKNIFTY26NOVFUT": "BANKNIFTY FUT · Nov", "RELIANCE-EQ": "RELIANCE", "ODD": "ODD"}
    for raw, want in cases.items():
        assert tgui.pretty_symbol(raw) == want, raw
    assert tgui.money(-6815.25) == "<b>▼ −₹6,815.25</b>" and tgui.money(None) == "<i>n/a</i>"
