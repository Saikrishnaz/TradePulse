"""Telegram UI: screens rendered as (HTML text, inline keyboard), navigated by editing one message in place.

Design rules
* One "control panel" message per chat: buttons re-render it instead of posting new messages.
* Every screen: a bold title with an emoji, an italic context line (scope · time), the content, and a
  keyboard ending in ⬅️ Home / ↻ Update. Lists page at PAGE items.
* Numbers read at a glance: signed money with ▲/▼ (never colour alone), readable instrument names,
  masked account ids.
* Callback data is ``v|view|account|arg|page`` (≤ 64 bytes). It is a *request*, never a permission:
  the account in it is re-checked against the chat's scope on every tap.
"""

from __future__ import annotations


from .config import now_ist, today_ist
from .localdata import mask_account
from .notify.format import broker_name, h, num, pretty_symbol  # noqa: F401 (re-exported)
from .service import Scope

PAGE = 6
VIEWS = ("home", "pos", "hold", "ord", "trd", "pnl", "sts", "rec", "ref", "rk", "acc", "od")
STATUS_ICON = {"COMPLETE": "✅", "PARTIALLY_FILLED": "🟡", "OPEN": "⏳", "SUBMITTING": "⏳", "ROUTED": "⏳", "UNKNOWN": "⚠️",
               "REJECTED": "❌", "FAILED": "❌", "CANCELLED": "🚫", "EXPIRED": "🚫"}
ORDER_FILTERS = (("all", "All", None), ("work", "⏳ Working", "PENDING"), ("fill", "✅ Filled", "FILLED"),
                 ("rej", "❌ Rejected", "REJECTED"))
PERIODS = (("today", "Today"), ("week", "Week"), ("month", "Month"))


# --- formatting tokens ------------------------------------------------------------------------------
def money(v, bold: bool = True) -> str:
    """Signed rupees with an arrow: ▲ +₹1,234.50 / ▼ −₹300.00 / ₹0.00 / n/a."""
    if v is None:
        return "<i>n/a</i>"
    f = float(v)
    if f > 0:
        t = f"▲ +₹{f:,.2f}"
    elif f < 0:
        t = f"▼ −₹{abs(f):,.2f}"
    else:
        t = "₹0.00"
    return f"<b>{t}</b>" if bold else t


def side_badge(side: str, qty: int | None = None) -> str:
    s = str(side or "").upper()
    dot = "🟢" if s in ("BUY", "LONG") else "🔴" if s in ("SELL", "SHORT") else "⚪"
    return f"{dot} <b>{h(s)}{' ' + format(int(qty), ',') if qty is not None else ''}</b>"


def hm(iso: str) -> str:
    return str(iso or "")[11:16] or "–"


def stamp() -> str:
    return now_ist().strftime("%H:%M:%S IST")


def acct_label(a: dict) -> str:
    return f"{(a.get('client_name') or '').split(' ')[0] or a['account_id']} {mask_account(a['account_id'])}"


# --- buttons ------------------------------------------------------------------------------------------
def cb(view: str, acct: str = "-", arg: str = "-", page: int = 0) -> str:
    data = f"v|{view}|{acct or '-'}|{arg or '-'}|{int(page)}"
    return data[:64]


def btn(text: str, view: str, acct: str = "-", arg: str = "-", page: int = 0) -> dict:
    return {"text": text, "callback_data": cb(view, acct, arg, page)}


def parse_cb(data: str) -> dict | None:
    parts = str(data or "").split("|")
    if len(parts) != 5 or parts[0] != "v" or parts[1] not in VIEWS:
        return None
    try:
        page = max(0, int(parts[4]))
    except ValueError:
        page = 0
    return {"view": parts[1], "acct": None if parts[2] == "-" else parts[2], "arg": None if parts[3] == "-" else parts[3],
            "page": page}


def kb(*rows) -> dict:
    return {"inline_keyboard": [list(r) for r in rows if r]}


def nav_row(view: str, acct: str | None, arg: str | None = None, page: int = 0) -> list:
    return [btn("⬅️ Home", "home"), btn("↻ Update", view, acct or "-", arg or "-", page)]


def account_row(view: str, acct: str | None, scope: Scope, svc, arg: str | None = None) -> list:
    if len(scope.account_ids) <= 1:
        return []
    label = "👤 All accounts ▾"
    if acct:
        a = svc.oms.account(acct)
        label = f"👤 {acct_label(a) if a else mask_account(acct)} ▾"
    return [btn(label, "acc", acct or "-", f"{view}.{arg or ''}")]


def pager(view: str, acct: str | None, arg: str | None, page: int, total: int) -> list:
    pages = max(1, -(-total // PAGE))
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn("◀ Prev", view, acct or "-", arg or "-", page - 1))
    row.append(btn(f"{page + 1} / {pages}", view, acct or "-", arg or "-", page))
    if page + 1 < pages:
        row.append(btn("Next ▶", view, acct or "-", arg or "-", page + 1))
    return row


def scope_label(svc, scope: Scope, acct: str | None) -> str:
    if acct:
        a = svc.oms.account(acct)
        return h(f"{a['client_name']} · {broker_name(a['broker'])} {mask_account(acct)}") if a else h(mask_account(acct))
    n = len(scope.account_ids)
    return f"All accounts ({n})" if n != 1 else h(scope.label)


def ids_for(scope: Scope, acct: str | None) -> list[str]:
    return [acct] if acct and scope.allows(acct) else list(scope.account_ids)


# --- screens --------------------------------------------------------------------------------------------
def home(svc, scope: Scope, **_) -> tuple[str, dict]:
    day = today_ist()
    ids = scope.account_ids
    c = svc.oms.order_counts(ids, day)["_all"]
    pnl = svc.oms.pnl(ids, ltp=svc.ltp)["total"]
    open_pos = len(svc.oms.positions(ids, "open"))
    trades = len(svc.oms.fills(ids, day, limit=10000))
    mon = len([a for a in ids if a in svc.store.monitored_ids()])
    text = "\n".join([
        "⚡ <b>TradePulse</b>",
        f"<i>{h(scope.label)} · {stamp()}</i>",
        "",
        f"💰 Net P&amp;L      {money(pnl['net'])}"
        + (f"  <i>({pnl['unpriced_open_legs']} unpriced)</i>" if pnl.get("unpriced_open_legs") else ""),
        f"🧾 Orders today   <b>{c['total']}</b>   ✅ {c['filled']}  ⏳ {c['pending']}  ❌ {c['rejected'] + c['failed']}",
        f"💹 Trades today   <b>{trades}</b>",
        f"📊 Open positions <b>{open_pos}</b>",
        f"👥 Accounts       <b>{len(ids)}</b>  ({mon} monitored)",
        "",
        "<i>Tap a section below.</i>",
    ])
    return text, kb(
        [btn("📊 Positions", "pos"), btn("🏦 Holdings", "hold")],
        [btn("🧾 Orders", "ord", arg="all"), btn("💹 Trades", "trd")],
        [btn("💰 P&L", "pnl", arg="today"), btn("📡 Status", "sts")],
        [btn("🕒 Recent", "rec"), btn("🔄 Refresh", "ref")],
        [btn("↻ Update", "home")])


def positions(svc, scope: Scope, acct=None, page=0, banner="", **_) -> tuple[str, dict]:
    ids = ids_for(scope, acct)
    rows = svc.oms.positions(ids, "open", ltp=svc.ltp)
    unpriced = sum(1 for r in rows if r["unrealized_pnl"] is None)
    total = sum(r["unrealized_pnl"] or 0 for r in rows) + sum(r["realized_pnl"] or 0 for r in rows)
    net_txt = money(total, bold=False) + (f" ({unpriced} unpriced)" if unpriced else "") if unpriced < len(rows) else "n/a"
    base = svc.oms.baselines(ids)
    ats = [b["positions"]["at"] for b in base.values() if "positions" in b]
    lines = [banner + "\n" if banner else "", f"📊 <b>Open positions</b> · {scope_label(svc, scope, acct)}",
             f"<i>{len(rows)} open · net {net_txt} · "
             f"{'snapshot ' + hm(min(ats)) + ' + live' if ats else 'from today’s trades'}</i>", ""]
    for r in rows[page * PAGE:(page + 1) * PAGE]:
        avg = num(r["average_entry_price"]) if r["average_entry_price"] is not None else "–"
        pnl_note = " <i>(broker)</i>" if r.get("pnl_source") == "broker" else ""
        lines += [f"<b>{h(pretty_symbol(r['symbol']))}</b>  {side_badge(r['direction'], abs(r['net_quantity']))}",
                  (f"   avg {avg} → <b>{num(r['ltp'])}</b>   {money(r['unrealized_pnl'])}{pnl_note}" if r["ltp"]
                   else f"   avg {avg} · <i>no live price</i>" + (f"   {money(r['unrealized_pnl'])}{pnl_note}" if r["unrealized_pnl"] is not None else "")),
                  f"   <i>{h(acct_label(r))} · {h(r['product'])}</i>", ""]
    if not rows:
        lines.append("<i>No open positions.</i>")
    return "\n".join(lines).strip(), kb(account_row("pos", acct, scope, svc), pager("pos", acct, None, page, len(rows)),
                                        [btn("🔄 Refresh from broker", "ref", acct or "-")] if acct else [],
                                        nav_row("pos", acct, page=page))


def holdings(svc, scope: Scope, acct=None, page=0, banner="", **_) -> tuple[str, dict]:
    ids = ids_for(scope, acct)
    rows = svc.oms.holdings(ids, ltp=svc.ltp)
    value = sum(r["value"] or 0 for r in rows)
    pnl = sum(r["pnl"] or 0 for r in rows)
    lines = [banner + "\n" if banner else "", f"🏦 <b>Holdings</b> · {scope_label(svc, scope, acct)}",
             f"<i>{len(rows)} stocks · value ₹{value:,.0f} · P&amp;L {money(pnl, bold=False)}</i>", ""]
    for r in rows[page * PAGE:(page + 1) * PAGE]:
        extra = []
        if r["t1_quantity"]:
            extra.append(f"T1 {r['t1_quantity']}")
        if r["pledged_quantity"]:
            extra.append(f"pledged {r['pledged_quantity']}")
        if r["change"]:
            extra.append(f"today {'+' if r['change'] > 0 else ''}{r['change']}")
        lines += [f"<b>{h(pretty_symbol(r['symbol']))}</b>  ×{r['quantity']:,}" + (f"  <i>({', '.join(extra)})</i>" if extra else ""),
                  (f"   avg {num(r['average_price'])} → <b>{num(r['ltp'])}</b>   {money(r['pnl'])}" if r["ltp"]
                   else f"   avg {num(r['average_price'])} · <i>no live price</i>"),
                  f"   <i>{h(acct_label(r))}</i>", ""]
    if not rows:
        lines.append("<i>No holdings recorded.</i>")
    return "\n".join(lines).strip(), kb(account_row("hold", acct, scope, svc), pager("hold", acct, None, page, len(rows)),
                                        [btn("🔄 Refresh from broker", "ref", acct or "-")] if acct else [],
                                        nav_row("hold", acct, page=page))


def orders(svc, scope: Scope, acct=None, arg=None, page=0, **_) -> tuple[str, dict]:
    flt = arg if arg in {f for f, _l, _s in ORDER_FILTERS} else "all"
    status = dict((f, s) for f, _l, s in ORDER_FILTERS)[flt]
    rows = svc.oms.orders(ids_for(scope, acct), day=today_ist(), status=status, limit=300)
    chunk = rows[page * PAGE:(page + 1) * PAGE]
    lines = [f"🧾 <b>Orders today</b> · {scope_label(svc, scope, acct)}",
             f"<i>{len(rows)} {dict((f, l) for f, l, _s in ORDER_FILTERS)[flt].split(' ')[-1].lower()} · {stamp()}</i>", ""]
    for i, o in enumerate(chunk, 1):
        lines += [f"{i}. {STATUS_ICON.get(o['status'], '•')} {hm(o['created_at'])}  {side_badge(o['side'], o['ordered_quantity'])} "
                  f"{h(pretty_symbol(o['symbol']))}",
                  f"   {o['filled_quantity']}/{o['ordered_quantity']} filled"
                  + (f" @ {num(o['average_price'])}" if o["average_price"] else "") + f" · <i>{h(acct_label(o))}</i>"]
        if o["status"] in ("REJECTED", "FAILED") and o.get("status_message"):
            lines.append(f"   <i>↳ {h(o['status_message'][:90])}</i>")
        lines.append("")
    if not rows:
        lines.append("<i>No orders match.</i>")
    chips = [btn(("✓ " if f == flt else "") + label, "ord", acct or "-", f) for f, label, _s in ORDER_FILTERS]
    details = [btn(f"🔍 {i}", "od", acct or "-", o["internal_order_id"]) for i, o in enumerate(chunk, 1)]
    return "\n".join(lines).strip(), kb(chips[:2], chips[2:], details[:3], details[3:], account_row("ord", acct, scope, svc, flt),
                                        pager("ord", acct, flt, page, len(rows)), nav_row("ord", acct, flt, page))


def order_detail(svc, scope: Scope, acct=None, arg=None, **_) -> tuple[str, dict]:
    o = svc.oms.order(scope.account_ids, arg or "")
    back = [btn("⬅️ Orders", "ord", acct or "-", "all"), btn("⬅️ Home", "home"), btn("↻ Update", "od", acct or "-", arg or "-")]
    if not o:
        return "🔍 <b>Order not found</b>\n<i>It is not in the accounts this chat can see.</i>", kb(back)
    icon = STATUS_ICON.get(o["status"], "•")
    lines = [f"{icon} <b>{h(o['status'].replace('_', ' ').title())}</b> · {h(broker_name(o['broker']))}",
             f"<b>{h(o['client_name'])}</b>  <code>{h(mask_account(o['account_id']))}</code>", "",
             f"{side_badge(o['side'], o['ordered_quantity'])} × <b>{h(pretty_symbol(o['symbol']))}</b>",
             f"{h(o['exchange'])} · {h(o['order_type'])} · {h(o['product'])} · limit {num(o['price'])}",
             f"Filled <b>{o['filled_quantity']}/{o['ordered_quantity']}</b>" + (f" @ <b>{num(o['average_price'])}</b>" if o["average_price"] else "")]
    if o.get("status_message"):
        lines.append(f"<blockquote>{h(o['status_message'][:300])}</blockquote>")
    if o["lifecycle"]:
        tl = "\n".join(f"{h(str(e['at'])[11:19])}  {h(e['from_status'] or '∅')} → <b>{h(e['to_status'])}</b>"
                       + (f"  <i>{h(e['message'][:60])}</i>" if e.get("message") else "") for e in o["lifecycle"][-14:])
        lines += ["", "<b>Lifecycle</b>", f"<blockquote expandable>{tl}</blockquote>"]
    if o["fills"]:
        lines += ["<b>Fills</b>"] + [f"  {f['quantity']} @ {num(f['price'])} · {h(str(f['filled_at'])[11:19])}" for f in o["fills"][:8]]
    lines += ["", f"<i>Order {h(o['broker_order_id'] or o['internal_order_id'])} · {stamp()}</i>"]
    return "\n".join(lines), kb(back)


def trades(svc, scope: Scope, acct=None, page=0, **_) -> tuple[str, dict]:
    rows = svc.oms.fills(ids_for(scope, acct), day=today_ist(), limit=500)
    lines = [f"💹 <b>Trades today</b> · {scope_label(svc, scope, acct)}", f"<i>{len(rows)} executions · {stamp()}</i>", ""]
    for f in rows[page * PAGE:(page + 1) * PAGE]:
        lines += [f"{hm(f['filled_at'])}  {side_badge(f['side'], f['quantity'])} {h(pretty_symbol(f['symbol']))} @ <b>{num(f['price'])}</b>",
                  f"   <i>{h(acct_label(f))}</i>", ""]
    if not rows:
        lines.append("<i>No trades executed today.</i>")
    return "\n".join(lines).strip(), kb(account_row("trd", acct, scope, svc), pager("trd", acct, None, page, len(rows)),
                                        nav_row("trd", acct, page=page))


def pnl(svc, scope: Scope, acct=None, arg=None, **_) -> tuple[str, dict]:
    period = arg if arg in {p for p, _ in PERIODS} else "today"
    perf = svc.performance(Scope(ids_for(scope, acct), scope.label), period)
    t = perf["total"]
    lines = [f"💰 <b>P&amp;L · {dict(PERIODS)[period]}</b> · {scope_label(svc, scope, acct)}", f"<i>since {h(perf['since'])} · {stamp()}</i>", "",
             f"Net         {money(t['net'])}",
             f"Realised    {money(t['realized'], bold=False)}",
             f"Unrealised  {money(t['unrealized'], bold=False)}",
             f"Open {t['open_legs']} · closed {t['closed_legs']} · win rate {t['win_rate'] if t['win_rate'] is not None else '–'}%"]
    if t.get("unpriced_open_legs"):
        lines.append(f"<i>{t['unpriced_open_legs']} open position(s) without a price are not included.</i>")
    rows = sorted([r for r in perf["accounts"] if r["net"] or r["open_legs"]], key=lambda r: r["net"])
    if len(rows) > 1:
        lines += ["", "<b>By account</b>"] + [f"{money(r['net'])}  <i>{h(acct_label(r))}</i>" for r in rows[:10]]
    chips = [btn(("✓ " if p == period else "") + label, "pnl", acct or "-", p) for p, label in PERIODS]
    return "\n".join(lines), kb(chips, account_row("pnl", acct, scope, svc, period), nav_row("pnl", acct, period))


def status(svc, scope: Scope, acct=None, page=0, **_) -> tuple[str, dict]:
    accts = svc.accounts(Scope(ids_for(scope, acct), scope.label))
    lines = [f"📡 <b>Account status</b> · {scope_label(svc, scope, acct)}", f"<i>{stamp()}</i>", ""]
    for a in accts[page * PAGE:(page + 1) * PAGE]:
        ok = a["session_status"] == "OK"
        lines += [f"{'🟢' if ok else '🔴'} <b>{h(a['client_name'])}</b> · {h(broker_name(a['broker']))} <code>{h(a['masked_account'])}</code>",
                  f"   session {h(a['session_status'])} · worker {h(a['worker_status'] or '–')} · "
                  f"{'monitored' if a['monitored'] else 'not monitored'}", ""]
    return "\n".join(lines).strip(), kb(pager("sts", acct, None, page, len(accts)), nav_row("sts", acct, page=page))


def recent(svc, scope: Scope, acct=None, page=0, **_) -> tuple[str, dict]:
    rows = svc.events(scope, account_id=acct if acct and scope.allows(acct) else None, limit=60)
    lines = [f"🕒 <b>Recent activity</b> · {scope_label(svc, scope, acct)}", f"<i>{stamp()}</i>", ""]
    from .notify.format import ICON
    for r in rows[page * PAGE:(page + 1) * PAGE]:
        what = r["type"].replace("ORDER_", "").replace("_", " ").title()
        lines += [f"{ICON.get(r['type'], '•')} {hm(r['occurred_at'])}  <b>{h(what)}</b> · {h(pretty_symbol(r['symbol']))}",
                  f"   <i>{h(acct_label(r))}</i>", ""]
    if not rows:
        lines.append("<i>No activity yet today.</i>")
    return "\n".join(lines).strip(), kb(account_row("rec", acct, scope, svc), pager("rec", acct, None, min(page, 9), len(rows)),
                                        nav_row("rec", acct, page=page))


def refresh_menu(svc, scope: Scope, acct=None, page=0, **_) -> tuple[str, dict]:
    if acct and scope.allows(acct):
        base = svc.oms.baselines([acct]).get(acct, {})
        text = "\n".join([f"🔄 <b>Refresh from broker</b> · {scope_label(svc, scope, acct)}", "",
                          f"📊 Positions  <i>last {hm((base.get('positions') or {}).get('at')) or 'never'}</i>",
                          f"🏦 Holdings   <i>last {hm((base.get('holdings') or {}).get('at')) or 'never'}</i>", "",
                          "<i>One read-only broker request every 3 seconds, shared by everyone.</i>"])
        return text, kb([btn("📊 Positions", "rk", acct, "positions"), btn("🏦 Holdings", "rk", acct, "holdings")],
                        [btn("⬅️ Accounts", "ref"), btn("⬅️ Home", "home")])
    accts = svc.oms.accounts(scope.account_ids)
    chunk = accts[page * 10:(page + 1) * 10]
    rows = [[btn(acct_label(a), "ref", a["account_id"]) for a in chunk[i:i + 2]] for i in range(0, len(chunk), 2)]
    pages = max(1, -(-len(accts) // 10))
    nav = []
    if page > 0:
        nav.append(btn("◀ Prev", "ref", page=page - 1))
    if page + 1 < pages:
        nav.append(btn("Next ▶", "ref", page=page + 1))
    return ("🔄 <b>Refresh from broker</b>\n<i>Choose an account.</i>", kb(*rows, nav, [btn("⬅️ Home", "home")]))


def account_picker(svc, scope: Scope, acct=None, arg=None, page=0, **_) -> tuple[str, dict]:
    """arg = 'view.viewarg' to return to."""
    target, _, targ = (arg or "home.").partition(".")
    accts = svc.oms.accounts(scope.account_ids)
    chunk = accts[page * 10:(page + 1) * 10]
    rows = [[btn(("✓ " if a["account_id"] == acct else "") + acct_label(a), target, a["account_id"], targ or "-")
             for a in chunk[i:i + 2]] for i in range(0, len(chunk), 2)]
    pages = max(1, -(-len(accts) // 10))
    nav = []
    if page > 0:
        nav.append(btn("◀ Prev", "acc", acct or "-", arg, page - 1))
    if page + 1 < pages:
        nav.append(btn("Next ▶", "acc", acct or "-", arg, page + 1))
    return ("👤 <b>Choose an account</b>", kb([btn(("✓ " if not acct else "") + "All accounts", target, "-", targ or "-")],
                                             *rows, nav, [btn("⬅️ Back", target, acct or "-", targ or "-")]))


SCREENS = {"home": home, "pos": positions, "hold": holdings, "ord": orders, "od": order_detail, "trd": trades,
           "pnl": pnl, "sts": status, "rec": recent, "ref": refresh_menu, "acc": account_picker}

# slash command -> (view, default arg)
COMMAND_VIEWS = {"start": ("home", None), "menu": ("home", None), "status": ("sts", None), "today": ("home", None),
                 "positions": ("pos", None), "holdings": ("hold", None), "orders": ("ord", "all"), "pending": ("ord", "work"),
                 "rejected": ("ord", "rej"), "trades": ("trd", None), "pnl": ("pnl", "today"), "recent": ("rec", None)}


def render(svc, scope: Scope, view: str, acct: str | None = None, arg: str | None = None, page: int = 0,
           banner: str = "") -> tuple[str, dict]:
    if acct and not scope.allows(acct):
        acct = None                                       # a forged/stale account in the button: fall back to all
    fn = SCREENS.get(view, home)
    return fn(svc, scope, acct=acct, arg=arg, page=page, banner=banner)


def event_keyboard(e: dict) -> dict | None:
    """Buttons under an alert card posted to a group."""
    row = []
    if e.get("internal_order_id"):
        row.append(btn("🔍 Order", "od", e.get("account_id") or "-", e["internal_order_id"]))
    if e.get("account_id"):
        row.append(btn("📊 Positions", "pos", e["account_id"]))
    row.append(btn("⚡ Menu", "home"))
    return kb(row)

