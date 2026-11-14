"""Telegram bot: linking, private vs group scope, registration, commands, mentions."""

from conftest import INSTRUMENTS, pump, setup_group, start_live
from tradepulse.bot import create_link_code

ADMIN_TG = {"id": 1001, "first_name": "Ad", "username": "ad"}
MATHEW_TG = {"id": 2002, "first_name": "Mathew", "username": "mat"}
STRANGER = {"id": 3003, "first_name": "S"}
PRIVATE = lambda u: {"id": u["id"], "type": "private"}  # noqa: E731
GROUP = {"id": -100777, "type": "supergroup", "title": "Mathew desk"}


def msg(bot, frm, chat, text, **extra):
    bot.handle_update({"update_id": 1, "message": {"message_id": 5, "from": frm, "chat": chat, "text": text, **extra}})


def link(rt, tg_user, username):
    u = rt.oms.user_by_username(username)
    code = create_link_code(rt.store, u["id"])["code"]
    msg(rt.bot, tg_user, PRIVATE(tg_user), f"/link {code}")
    return code


def test_unlinked_private_user_gets_no_data(rt, tg):
    msg(rt.bot, STRANGER, PRIVATE(STRANGER), "/positions")
    assert "isn't linked" in tg.texts()[-1]
    msg(rt.bot, STRANGER, PRIVATE(STRANGER), "what are my positions?")      # free text: command list only, no data
    assert "/positions" in tg.texts()[-1] and "NIFTY" not in tg.texts()[-1]


def test_link_code_single_use_and_scoped_answers(rt, tg):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="fill", instrument=INSTRUMENTS[0])
    rt.sim.order("J252505", outcome="fill", instrument=INSTRUMENTS[2])
    pump(rt)
    code = link(rt, MATHEW_TG, "mathew")
    assert "Linked to <b>Mathew</b>" in tg.texts()[-1]
    msg(rt.bot, STRANGER, PRIVATE(STRANGER), f"/link {code}")              # reused code
    assert "invalid or has expired" in tg.texts()[-1]
    msg(rt.bot, MATHEW_TG, PRIVATE(MATHEW_TG), "/positions")
    t = tg.texts()[-1]
    assert "NIFTY26NOV26000CE" in t and "BANKNIFTY" not in t               # Jayesh's position is invisible
    msg(rt.bot, MATHEW_TG, PRIVATE(MATHEW_TG), "/positions jayesh")
    assert "No account matching" in tg.texts()[-1]


def test_link_only_in_private_chat(rt, tg):
    u = rt.oms.user_by_username("mathew")
    code = create_link_code(rt.store, u["id"])["code"]
    msg(rt.bot, MATHEW_TG, GROUP, f"/link {code}")
    assert "private chat" in tg.texts()[-1]
    assert rt.store.one("SELECT used_at FROM link_codes WHERE code = ?", (code,))["used_at"] == ""


def test_bot_added_to_group_is_pending_until_admin_registers(rt, tg):
    rt.bot.handle_update({"update_id": 2, "my_chat_member": {"chat": GROUP, "from": ADMIN_TG,
                                                             "new_chat_member": {"status": "member"}}})
    ch = rt.store.channel_by_chat("telegram", str(GROUP["id"]))
    assert ch["status"] == "PENDING" and ch["enabled"] == 0
    msg(rt.bot, MATHEW_TG, GROUP, "/status")
    assert "isn't connected" in tg.texts()[-1]
    msg(rt.bot, MATHEW_TG, GROUP, "/register")                              # not an admin
    assert "Only a linked TradePulse admin" in tg.texts()[-1]
    link(rt, ADMIN_TG, "admin")
    msg(rt.bot, ADMIN_TG, GROUP, "/register")
    ch = rt.store.channel_by_chat("telegram", str(GROUP["id"]))
    assert ch["status"] == "ACTIVE" and ch["enabled"] == 1


def test_group_scope_is_the_groups_accounts_whoever_asks(rt, tg):
    start_live(rt)
    rt.sim.order("ZM4521", outcome="fill", instrument=INSTRUMENTS[0])
    rt.sim.order("BR77104", outcome="fill", instrument=INSTRUMENTS[2])
    pump(rt)
    setup_group(rt, ["ZM4521"], chat_id=str(GROUP["id"]), name="Mathew desk")
    link(rt, ADMIN_TG, "admin")                                             # even an admin is limited to the group
    msg(rt.bot, ADMIN_TG, GROUP, "/positions")
    t = tg.texts()[-1]
    assert "NIFTY26NOV26000CE" in t and "BANKNIFTY" not in t
    msg(rt.bot, STRANGER, GROUP, "/today")                                  # an unlinked member may ask about the group
    assert "Today's trading" in tg.texts()[-1]


def test_group_queries_can_be_disabled(rt, tg):
    setup_group(rt, ["ZM4521"], chat_id=str(GROUP["id"]), allow_queries=0)
    msg(rt.bot, STRANGER, GROUP, "/status")
    assert "turned off" in tg.texts()[-1]


def test_group_chatter_ignored_mention_gets_command_list(rt, tg):
    """No AI: ordinary group messages are ignored; mentioning the bot returns the command list."""
    setup_group(rt, ["ZM4521"], chat_id=str(GROUP["id"]))
    rt.bot.username = "TradePulseBot"
    msg(rt.bot, STRANGER, GROUP, "hello everyone")
    assert tg.sent == []
    msg(rt.bot, STRANGER, GROUP, "@TradePulseBot what is the status of the accounts?")
    assert tg.sent and "/status" in tg.texts()[-1] and "/help" in tg.texts()[-1]
    assert tg.sent[-1]["reply_to"] == 5


def test_ask_command_is_gone(rt, tg):
    setup_group(rt, ["ZM4521"], chat_id=str(GROUP["id"]))
    msg(rt.bot, STRANGER, GROUP, "/ask what happened today")
    assert "Unknown command" in tg.texts()[-1]
    msg(rt.bot, STRANGER, GROUP, "/help")
    assert "/ask" not in tg.texts()[-1]


def test_commands_for_other_bots_ignored(rt, tg):
    setup_group(rt, ["ZM4521"], chat_id=str(GROUP["id"]))
    rt.bot.username = "TradePulseBot"
    msg(rt.bot, STRANGER, GROUP, "/status@OtherBot")
    assert tg.sent == []


def test_order_command_and_help(rt, tg):
    start_live(rt)
    r = rt.sim.order("ZM4521", outcome="reject", reject_message="RMS: insufficient margin")
    pump(rt)
    link(rt, MATHEW_TG, "mathew")
    msg(rt.bot, MATHEW_TG, PRIVATE(MATHEW_TG), f"/order {r['internal_order_id']}")
    t = tg.texts()[-1]
    assert "Lifecycle" in t and "REJECTED" in t and "insufficient margin" in t
    msg(rt.bot, MATHEW_TG, PRIVATE(MATHEW_TG), "/help")
    assert "/positions" in tg.texts()[-1]


def test_bot_removed_marks_channel(rt, tg):
    setup_group(rt, ["ZM4521"], chat_id=str(GROUP["id"]))
    rt.bot.handle_update({"update_id": 3, "my_chat_member": {"chat": GROUP, "from": ADMIN_TG, "new_chat_member": {"status": "kicked"}}})
    ch = rt.store.channel_by_chat("telegram", str(GROUP["id"]))
    assert ch["status"] == "REMOVED" and ch["enabled"] == 0


def test_group_migration_keeps_channel(rt, tg):
    setup_group(rt, ["ZM4521"], chat_id="-555")
    msg(rt.bot, STRANGER, {"id": -555, "type": "group", "title": "x"}, "", migrate_to_chat_id=-100555)
    assert rt.store.channel_by_chat("telegram", "-100555") is not None


def test_disabled_oms_user_loses_bot_access(rt, tg):
    link(rt, MATHEW_TG, "mathew")
    rt.store.execute("UPDATE users SET status = 'DISABLED' WHERE username = 'mathew'")
    msg(rt.bot, MATHEW_TG, PRIVATE(MATHEW_TG), "/status")
    assert "isn't linked" in tg.texts()[-1]
