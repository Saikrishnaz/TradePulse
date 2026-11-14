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
from .notify.format import show_id
from .notify.format import broker_name, h, num, pretty_symbol  # noqa: F401 (re-exported)
from .service import Scope

PAGE = 6
VIEWS = ("home", "pos", "hold", "ord", "trd", "pnl", "sts", "rec", "ref", "rk", "acc", "od", "ac")
HOME_ACCOUNTS = 8                                         # account rows/buttons on a group's home screen
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
    return f"{(a.get('client_name') or '').split(' ')[0] or a['account_id']} {show_id(a['account_id'])}"


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
        label = f"👤 {acct_label(a) if a else show_id(acct)} ▾"
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
        return h(f"{a['client_name']} · {broker_name(a['broker'])} {show_id(acct)}") if a else h(show_id(acct))
    n = len(scope.account_ids)
    return f"All accounts ({n})" if n != 1 else h(scope.label)


def ids_for(scope: Scope, acct: str | None) -> list[str]:
    return [acct] if acct and scope.allows(acct) else list(scope.account_ids)


def book_for(svc, scope: Scope, acct: str | None) -> list[str]:
    """Accounts whose positions/holdings this chat may see (a LIVE-only group never sees a paper book)."""
    return svc.book_ids(scope, ids_for(scope, acct))


def mode_tag(scope: Scope, mode: str | None) -> str:
    """Mark non-LIVE rows where a chat can see more than one mode."""
    m = str(mode or "").upper()
    if not m or m == "LIVE" or (scope.modes is not None and len(scope.modes) <= 1):
        return ""
    return f" 🧪{h(m)}"


def modes_label(scope: Scope) -> str:
    if scope.modes is None:
        return "all modes"
    return " + ".join(scope.modes) if scope.modes else "no modes"


def session_dot(a: dict) -> str:
    return "🟢" if a.get("session_status") == "OK" else "🔴"


def account_snapshot(svc, scope: Scope, ids: list[str]) -> dict:
    """Per-account numbers for the home screens and the account card, all inside the chat's scope."""
    day = today_ist()
    counts = svc.oms.order_counts(ids, day, modes=scope.modes)
    book = svc.book_ids(scope, ids)
    pnl = svc.oms.pnl(book, ltp=svc.ltp)
    fills = svc.oms.fills(ids, day, limit=10000, modes=scope.modes)
    trades: dict = {}
    for f in fills:
        trades[f["account_id"]] = trades.get(f["account_id"], 0) + 1
    zero = {"total": 0, "filled": 0, "partial": 0, "pending": 0, "rejected": 0, "cancelled": 0, "failed": 0}
    accts = {a["account_id"]: a for a in svc.accounts(scope.narrow(ids))}
    rows = []
    for aid in ids:
        a = accts.get(aid)
        if not a:
            continue
        p = pnl["accounts"].get(aid) or {"net": 0.0, "open_legs": 0, "realized": 0.0, "unrealized": 0.0}
        rows.append({**a, "net": p["net"] if aid in book else None, "open_legs": p["open_legs"] if aid in book else 0,
                     "realized": p.get("realized"), "unrealized": p.get("unrealized"),
                     "orders": counts.get(aid, zero), "trades": trades.get(aid, 0)})
    return {"rows": rows, "total": pnl["total"], "counts": counts["_all"], "trades": len(fills)}


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def counts_line(c: dict) -> str:
    return f"✅ {c['filled']}  ⏳ {c['pending']}  ❌ {c['rejected'] + c['failed']}"


# --- screens --------------------------------------------------------------------------------------------
def home(svc, scope: Scope, **kw) -> tuple[str, dict]:
    return group_home(svc, scope, **kw) if scope.is_group else private_home(svc, scope, **kw)


def group_home(svc, scope: Scope, page=0, **_) -> tuple[str, dict]:
    """A group's desk: one line per account the group follows, then the group total. Only this group's
    accounts and trading modes - nothing about the rest of the firm."""
    snap = account_snapshot(svc, scope, list(scope.account_ids))
    rows = sorted(snap["rows"], key=lambda r: (-(abs(r["net"] or 0)), r["client_name"] or ""))
    shown = rows[:HOME_ACCOUNTS]
    lines = [f"👥 <b>{h(scope.name or scope.label)}</b>",
             f"<i>{len(rows)} account{'s' if len(rows) != 1 else ''} · {h(modes_label(scope))} · {stamp()}</i>", ""]
    for r in shown:
        c = r["orders"]
        bits = [money(r["net"]) if r["net"] is not None else "<i>no live book</i>",
                f"{r['open_legs']} open" if r["open_legs"] else "flat",
                f"{c['total']} order{'s' if c['total'] != 1 else ''}" if c["total"] else "no orders"]
        if c["rejected"] + c["failed"]:
            bits.append(f"❌ {c['rejected'] + c['failed']}")
        lines += [f"{session_dot(r)} <b>{h(r['client_name'] or r['account_id'])}</b> · {h(broker_name(r['broker']))} "
                  f"<code>{h(r['account_id'])}</code>",
                  "      " + " · ".join(bits)]
    if len(rows) > len(shown):
        lines.append(f"<i>+ {len(rows) - len(shown)} more account(s) — tap 👤 All accounts</i>")
    if not rows:
        lines.append("<i>This group follows no accounts yet.</i>")
    t = snap["total"]
    lines += ["━━━━━━━━━━━━━━",
              f"Σ <b>Group</b>  {money(t['net'])}"
              + (f"  <i>({t['unpriced_open_legs']} unpriced)</i>" if t.get("unpriced_open_legs") else ""),
              f"🧾 {plural(snap['counts']['total'], 'order')}  {counts_line(snap['counts'])}",
              f"💹 {plural(snap['trades'], 'trade')} · 📊 {t['open_legs']} open"]
    acct_btns = [btn(f"👤 {acct_label(r)}", "ac", r["account_id"]) for r in shown]
    acct_rows = [acct_btns[i:i + 2] for i in range(0, len(acct_btns), 2)]
    more = [btn("👤 All accounts ▾", "acc", "-", "ac.")] if len(rows) > len(shown) else []
    return "\n".join(lines), kb(
        *acct_rows, more,
        [btn("📊 Positions", "pos"), btn("🧾 Orders", "ord", arg="all")],
        [btn("💹 Trades", "trd"), btn("💰 P&L", "pnl", arg="today")],
        [btn("🕒 Activity", "rec"), btn("🔄 Refresh", "ref")],
        [btn("↻ Update", "home")])


def private_home(svc, scope: Scope, **_) -> tuple[str, dict]:
    """A person's own desk: their totals across everything they may see, the biggest movers, and the
    way into any single account."""
    snap = account_snapshot(svc, scope, list(scope.account_ids))
    user = scope.user or {}
    t = snap["total"]
    mon = sum(1 for r in snap["rows"] if r.get("monitored"))
    down = sum(1 for r in snap["rows"] if r.get("monitored") and r.get("session_status") != "OK")
    lines = ["⚡ <b>TradePulse</b> · your desk",
             f"👋 <b>{h(user.get('name') or scope.label)}</b>" + (f" · {h(user.get('role'))}" if user.get("role") else ""),
             f"<i>{stamp()}</i>", "",
             f"💰 Net P&amp;L  {money(t['net'])}"
             + (f"  <i>({t['unpriced_open_legs']} unpriced)</i>" if t.get("unpriced_open_legs") else ""),
             f"🧾 Orders <b>{snap['counts']['total']}</b>  {counts_line(snap['counts'])}",
             f"💹 Trades <b>{snap['trades']}</b> · 📊 Open <b>{t['open_legs']}</b>",
             f"👥 Accounts <b>{len(snap['rows'])}</b> · {mon} monitored"
             + (f" · ⚠️ {down} session{'s' if down != 1 else ''} down" if down else "")]
    movers = sorted([r for r in snap["rows"] if r["net"] or r["orders"]["total"]], key=lambda r: -abs(r["net"] or 0))[:4]
    if movers:
        lines += ["", "<b>Today’s movers</b>"]
        lines += [f"{money(r['net'])}  {h(acct_label(r))} · {h(broker_name(r['broker']))}" for r in movers]
    return "\n".join(lines), kb(
        [btn(f"👤 {acct_label(r)}", "ac", r["account_id"]) for r in movers[:2]],
        [btn("📊 Positions", "pos"), btn("🏦 Holdings", "hold")],
        [btn("🧾 Orders", "ord", arg="all"), btn("💹 Trades", "trd")],
        [btn("💰 P&L", "pnl", arg="today"), btn("📡 Status", "sts")],
        [btn("👤 Accounts", "acc", "-", "ac."), btn("🕒 Recent", "rec")],
        [btn("🔄 Refresh", "ref"), btn("↻ Update", "home")])


def account_card(svc, scope: Scope, acct=None, page=0, **_) -> tuple[str, dict]:
    """Everything about one account on one screen, with that account's own buttons."""
    if not acct:
        return account_picker(svc, scope, None, "ac.", page)
    snap = account_snapshot(svc, scope, [acct])
    if not snap["rows"]:
        return "👤 <b>Account not available</b>", kb([btn("⬅️ Home", "home")])
    r = snap["rows"][0]
    c = r["orders"]
    lines = [f"👤 <b>{h(r['client_name'] or acct)}</b>",
             f"{h(broker_name(r['broker']))} <code>{h(r['account_id'])}</code> · {session_dot(r)} session "
             f"{h(r['session_status'])} · worker {h(r['worker_status'] or '–')}",
             f"<i>{h(modes_label(scope)) + ' · ' if scope.modes is not None else ''}{stamp()}</i>", ""]
    if r["net"] is None:
        lines.append("💰 <i>This account’s book is not shown in this chat.</i>")
    else:
        lines.append(f"💰 Net  {money(r['net'])}   <i>realised {money(r['realized'], bold=False)} · "
                     f"open {money(r['unrealized'], bold=False)}</i>")
    lines += [f"🧾 Orders <b>{c['total']}</b>  {counts_line(c)}", f"💹 Trades <b>{r['trades']}</b>"]
    pos = svc.oms.positions(svc.book_ids(scope, [acct]), "open", ltp=svc.ltp)
    lines.append(f"📊 Open <b>{len(pos)}</b>")
    for p in sorted(pos, key=lambda x: -abs(x["unrealized_pnl"] or 0))[:4]:
        lines.append(f"   {side_badge(p['direction'], abs(p['net_quantity']))} {h(pretty_symbol(p['symbol']))}  "
                     f"{money(p['unrealized_pnl'], bold=False)}")
    if len(pos) > 4:
        lines.append(f"   <i>+ {len(pos) - 4} more</i>")
    last = svc.events(scope, account_id=acct, limit=1)
    if last:
        from .notify.format import ICON
        e = last[0]
        lines += ["", f"🕒 Last: {ICON.get(e['type'], '•')} {hm(e['occurred_at'])} "
                      f"{h(e['type'].replace('ORDER_', '').replace('_', ' ').title())} · {h(pretty_symbol(e['symbol']))}"]
    return "\n".join(lines), kb(
        [btn("📊 Positions", "pos", acct), btn("🏦 Holdings", "hold", acct)],
        [btn("🧾 Orders", "ord", acct, "all"), btn("💹 Trades", "trd", acct)],
        [btn("💰 P&L", "pnl", acct, "today"), btn("🕒 Activity", "rec", acct)],
        [btn("🔄 Refresh from broker", "ref", acct)],
        [btn("⬅️ Home", "home"), btn("↻ Update", "ac", acct)])


def pct(now, base) -> str:
    try:
        if not base:
            return ""
        v = (float(now) - float(base)) / float(base) * 100
    except (TypeError, ValueError):
        return ""
    return f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):.1f}%"


def rupees(v) -> str:
    return "–" if v is None else f"₹{float(v):,.0f}"


def by_account(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r["account_id"], []).append(r)
    return out


def account_head(r: dict, extra: str) -> list[str]:
    name = (r.get("client_name") or r["account_id"])
    return [f"👤 <b>{h(name)}</b>  <code>{h(show_id(r['account_id']))}</code> · {h(broker_name(r.get('broker')))}",
            f"<i>{extra}</i>"]


def _pos_pnl(r: dict) -> float:
    return (r["unrealized_pnl"] or 0) + (r["realized_pnl"] or 0)


def positions(svc, scope: Scope, acct=None, page=0, banner="", **_) -> tuple[str, dict]:
    """Summary first (net, long/short, best/worst), then one block per account: its subtotal and its
    positions, worst first, so a reader sees which client is hurting at a glance."""
    ids = book_for(svc, scope, acct)
    rows = svc.oms.positions(ids, "open", ltp=svc.ltp)
    priced = [r for r in rows if r["unrealized_pnl"] is not None]
    unpriced = len(rows) - len(priced)
    total = sum(_pos_pnl(r) for r in rows)
    base = svc.oms.baselines(ids)
    ats = [b["positions"]["at"] for b in base.values() if "positions" in b]
    groups = by_account(rows)
    lines = [banner + "\n" if banner else "", f"📊 <b>Open positions</b> · {scope_label(svc, scope, acct)}",
             f"<i>{'broker ' + hm(min(ats)) + ' + live fills' if ats else 'from today’s trades'} · {stamp()}</i>", ""]
    if rows:
        longs = sum(1 for r in rows if r["direction"] == "LONG")
        lines += [f"💰 Net  {money(total) if priced else '<i>n/a</i>'}" + (f"  <i>({unpriced} unpriced)</i>" if unpriced else ""),
                  f"🟢 {longs} long · 🔴 {len(rows) - longs} short" + (f" · 👥 {len(groups)} accounts" if len(groups) > 1 else "")]
        if len(priced) > 1:
            best, worst = max(priced, key=_pos_pnl), min(priced, key=_pos_pnl)
            if _pos_pnl(best) > 0:
                lines.append(f"🏆 {h(pretty_symbol(best['symbol']))}  {money(_pos_pnl(best), bold=False)}")
            if _pos_pnl(worst) < 0:
                lines.append(f"⚠️ {h(pretty_symbol(worst['symbol']))}  {money(_pos_pnl(worst), bold=False)}")
    # worst account first, worst position first inside it
    order = sorted(groups, key=lambda k: sum(_pos_pnl(r) for r in groups[k]))
    flat = [r for k in order for r in sorted(groups[k], key=_pos_pnl)]
    chunk = flat[page * PAGE:(page + 1) * PAGE]
    for aid, items in by_account(chunk).items():
        allr = groups[aid]
        sub = (money(sum(_pos_pnl(r) for r in allr), bold=False) if any(r["unrealized_pnl"] is not None for r in allr)
               else "P&amp;L n/a")
        lines += [""] + account_head(allr[0], f"{plural(len(allr), 'position')} · {sub}")
        cards = []
        for r in items:
            dot = "🟢" if r["direction"] == "LONG" else "🔴"
            avg = r["average_entry_price"]
            price = (f"avg {num(avg)} → <b>{num(r['ltp'])}</b>" + (f" ({pct(r['ltp'], avg)})" if avg else "")
                     if avg is not None and r["ltp"] else f"LTP <b>{num(r['ltp'])}</b>" if r["ltp"] else "<i>no live price</i>")
            src = "  <i>broker P&amp;L</i>" if r.get("pnl_source") == "broker" else ""
            cards.append("\n".join([
                f"{dot} <b>{h(pretty_symbol(r['symbol']))}</b>",
                f"{h(r['direction'])} {abs(r['net_quantity']):,} · {h(r['product'])} · {price}",
                (money(_pos_pnl(r)) if r["unrealized_pnl"] is not None else "<i>P&amp;L n/a</i>") + src]))
        lines.append("<blockquote>" + "\n\n".join(cards) + "</blockquote>")
    if not rows:
        lines.append("<i>No open positions.</i>")
    return "\n".join(lines).strip(), kb(account_row("pos", acct, scope, svc), pager("pos", acct, None, page, len(rows)),
                                        [btn("🔄 Refresh from broker", "ref", acct or "-")] if acct else [],
                                        [btn("🏦 Holdings", "hold", acct or "-")] if not scope.is_group else [],
                                        nav_row("pos", acct, page=page))


def holdings(svc, scope: Scope, acct=None, page=0, banner="", **_) -> tuple[str, dict]:
    """Portfolio summary (invested → value, P&L %, gainers/losers), then one block per account."""
    ids = book_for(svc, scope, acct)
    rows = svc.oms.holdings(ids, ltp=svc.ltp)
    priced = [r for r in rows if r["value"] is not None]
    invested = sum(r["average_price"] * r["quantity"] for r in priced)
    value = sum(r["value"] for r in priced)
    pnl = sum(r["pnl"] or 0 for r in priced)
    base = svc.oms.baselines(ids)
    ats = [b["holdings"]["at"] for b in base.values() if "holdings" in b]
    groups = by_account(rows)
    lines = [banner + "\n" if banner else "", f"🏦 <b>Holdings</b> · {scope_label(svc, scope, acct)}",
             f"<i>{'broker ' + hm(min(ats)) + ' + live fills' if ats else 'from today’s trades'} · {stamp()}</i>", ""]
    if rows:
        up = sum(1 for r in priced if (r["pnl"] or 0) > 0)
        cost = sum(r["average_price"] * r["quantity"] for r in rows)
        lines += ([f"💼 Invested {rupees(invested)} → <b>{rupees(value)}</b>",
                   f"💰 P&amp;L  {money(pnl)}" + (f"  <i>({pct(value, invested)})</i>" if invested else "")] if priced
                  else [f"💼 Cost <b>{rupees(cost)}</b>  <i>(no live prices yet)</i>"]) + [
                  f"📈 {up} up · 📉 {len(priced) - up} down" + (f" · {len(rows) - len(priced)} unpriced" if len(priced) < len(rows) else "")
                  + (f" · 👥 {len(groups)} accounts" if len(groups) > 1 else "")]
    order = sorted(groups, key=lambda k: -sum(r["value"] or 0 for r in groups[k]))
    flat = [r for k in order for r in sorted(groups[k], key=lambda r: -(r["value"] or 0))]
    chunk = flat[page * PAGE:(page + 1) * PAGE]
    for aid, items in by_account(chunk).items():
        allr = groups[aid]
        av = sum(r["value"] or 0 for r in allr)
        ap = sum(r["pnl"] or 0 for r in allr)
        if any(r["value"] is not None for r in allr):
            sub = f"{rupees(av)} · {money(ap, bold=False)}"
        else:
            sub = f"cost {rupees(sum(r['average_price'] * r['quantity'] for r in allr))}"
        lines += [""] + account_head(allr[0], f"{plural(len(allr), 'stock')} · {sub}")
        cards = []
        for r in items:
            g = r["pnl"]
            icon = "📈" if (g or 0) > 0 else "📉" if (g or 0) < 0 else "▫️"
            extra = []
            if r["t1_quantity"]:
                extra.append(f"T1 {r['t1_quantity']}")
            if r["pledged_quantity"]:
                extra.append(f"pledged {r['pledged_quantity']}")
            if r["change"]:
                extra.append(f"{'bought' if r['change'] > 0 else 'sold'} {abs(r['change'])} today")
            cards.append("\n".join([
                f"{icon} <b>{h(pretty_symbol(r['symbol']))}</b> ×{r['quantity']:,}" + (f"  <i>{h(' · '.join(extra))}</i>" if extra else ""),
                (f"avg {num(r['average_price'])} → <b>{num(r['ltp'])}</b> · {rupees(r['value'])}" if r["ltp"]
                 else f"avg {num(r['average_price'])} · <i>no live price</i>"),
                (money(g) + f"  <i>({pct(r['ltp'], r['average_price'])})</i>") if g is not None else "<i>P&amp;L n/a</i>"]))
        lines.append("<blockquote>" + "\n\n".join(cards) + "</blockquote>")
    if not rows:
        lines.append("<i>No holdings recorded.</i>")
    return "\n".join(lines).strip(), kb(account_row("hold", acct, scope, svc), pager("hold", acct, None, page, len(rows)),
                                        [btn("🔄 Refresh from broker", "ref", acct or "-")] if acct else [],
                                        [btn("📊 Positions", "pos", acct or "-")] if not scope.is_group else [],
                                        nav_row("hold", acct, page=page))


def orders(svc, scope: Scope, acct=None, arg=None, page=0, **_) -> tuple[str, dict]:
    flt = arg if arg in {f for f, _l, _s in ORDER_FILTERS} else "all"
    status = dict((f, s) for f, _l, s in ORDER_FILTERS)[flt]
    rows = svc.oms.orders(ids_for(scope, acct), day=today_ist(), status=status, limit=300, modes=scope.modes)
    chunk = rows[page * PAGE:(page + 1) * PAGE]
    lines = [f"🧾 <b>Orders today</b> · {scope_label(svc, scope, acct)}",
             f"<i>{len(rows)} {dict((f, l) for f, l, _s in ORDER_FILTERS)[flt].split(' ')[-1].lower()} · {stamp()}</i>", ""]
    for i, o in enumerate(chunk, 1):
        lines += [f"{i}. {STATUS_ICON.get(o['status'], '•')} {hm(o['created_at'])}  {side_badge(o['side'], o['ordered_quantity'])} "
                  f"{h(pretty_symbol(o['symbol']))}{mode_tag(scope, o.get('trading_mode'))}",
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
    o = svc.oms.order(scope.account_ids, arg or "", modes=scope.modes)
    back = [btn("⬅️ Orders", "ord", acct or "-", "all"), btn("⬅️ Home", "home"), btn("↻ Update", "od", acct or "-", arg or "-")]
    if not o:
        return "🔍 <b>Order not found</b>\n<i>It is not in the accounts this chat can see.</i>", kb(back)
    icon = STATUS_ICON.get(o["status"], "•")
    lines = [f"{icon} <b>{h(o['status'].replace('_', ' ').title())}</b> · {h(broker_name(o['broker']))}",
             f"<b>{h(o['client_name'])}</b>  <code>{h(show_id(o['account_id']))}</code>", "",
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
    rows = svc.oms.fills(ids_for(scope, acct), day=today_ist(), limit=500, modes=scope.modes)
    lines = [f"💹 <b>Trades today</b> · {scope_label(svc, scope, acct)}", f"<i>{len(rows)} executions · {stamp()}</i>", ""]
    for f in rows[page * PAGE:(page + 1) * PAGE]:
        lines += [f"{hm(f['filled_at'])}  {side_badge(f['side'], f['quantity'])} {h(pretty_symbol(f['symbol']))} @ <b>{num(f['price'])}</b>{mode_tag(scope, f.get('trading_mode'))}",
                  f"   <i>{h(acct_label(f))}</i>", ""]
    if not rows:
        lines.append("<i>No trades executed today.</i>")
    return "\n".join(lines).strip(), kb(account_row("trd", acct, scope, svc), pager("trd", acct, None, page, len(rows)),
                                        nav_row("trd", acct, page=page))


def pnl(svc, scope: Scope, acct=None, arg=None, **_) -> tuple[str, dict]:
    period = arg if arg in {p for p, _ in PERIODS} else "today"
    perf = svc.performance(scope.narrow(ids_for(scope, acct)), period)
    t = perf["total"]
    lines = [f"💰 <b>P&amp;L · {dict(PERIODS)[period]}</b> · {scope_label(svc, scope, acct)}", f"<i>since {h(perf['since'])} · {stamp()}</i>", "",
             f"Net         {money(t['net'])}",
             f"Realised    {money(t['realized'], bold=False)}",
             f"Unrealised  {money(t['unrealized'], bold=False)}",
             f"Open {t['open_legs']} · closed {t['closed_legs']} · win rate {t['win_rate'] if t['win_rate'] is not None else '–'}%"]
    if t.get("unpriced_open_legs"):
        lines.append(f"<i>{t['unpriced_open_legs']} open position(s) without a price are not included.</i>")
    visible = set(svc.book_ids(scope, [x["account_id"] for x in perf["accounts"]]))
    rows = sorted([r for r in perf["accounts"] if r["account_id"] in visible and (r["net"] or r["open_legs"])],
                  key=lambda r: r["net"])
    if len(rows) > 1:
        lines += ["", "<b>By account</b>"] + [f"{money(r['net'])}  <i>{h(acct_label(r))}</i>" for r in rows[:10]]
    chips = [btn(("✓ " if p == period else "") + label, "pnl", acct or "-", p) for p, label in PERIODS]
    return "\n".join(lines), kb(chips, account_row("pnl", acct, scope, svc, period), nav_row("pnl", acct, period))


def status(svc, scope: Scope, acct=None, page=0, **_) -> tuple[str, dict]:
    accts = svc.accounts(scope.narrow(ids_for(scope, acct)))
    lines = [f"📡 <b>Account status</b> · {scope_label(svc, scope, acct)}", f"<i>{stamp()}</i>", ""]
    for a in accts[page * PAGE:(page + 1) * PAGE]:
        ok = a["session_status"] == "OK"
        lines += [f"{'🟢' if ok else '🔴'} <b>{h(a['client_name'])}</b> · {h(broker_name(a['broker']))} <code>{h(a['account_id'])}</code>",
                  f"   session {h(a['session_status'])} · worker {h(a['worker_status'] or '–')} · "
                  f"{'monitored' if a['monitored'] else 'not monitored'}", ""]
    cards = [btn(f"👤 {acct_label(a)}", "ac", a["account_id"]) for a in accts[page * PAGE:(page + 1) * PAGE]]
    return "\n".join(lines).strip(), kb(*[cards[i:i + 2] for i in range(0, len(cards), 2)],
                                        pager("sts", acct, None, page, len(accts)), nav_row("sts", acct, page=page))


def recent(svc, scope: Scope, acct=None, page=0, **_) -> tuple[str, dict]:
    rows = svc.events(scope, account_id=acct if acct and scope.allows(acct) else None, limit=60)
    lines = [f"🕒 <b>Recent activity</b> · {scope_label(svc, scope, acct)}", f"<i>{stamp()}</i>", ""]
    from .notify.format import ICON
    for r in rows[page * PAGE:(page + 1) * PAGE]:
        what = r["type"].replace("ORDER_", "").replace("_", " ").title()
        lines += [f"{ICON.get(r['type'], '•')} {hm(r['occurred_at'])}  <b>{h(what)}</b> · {h(pretty_symbol(r['symbol']))}"
                  f"{mode_tag(scope, r.get('trading_mode'))}",
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
    top = [] if target == "ac" else [btn(("✓ " if not acct else "") + "All accounts", target, "-", targ or "-")]
    back = [btn("⬅️ Home", "home")] if target == "ac" else [btn("⬅️ Back", target, acct or "-", targ or "-")]
    return ("👤 <b>Choose an account</b>", kb(top,
                                             *rows, nav, back))


SCREENS = {"home": home, "pos": positions, "hold": holdings, "ord": orders, "od": order_detail, "trd": trades,
           "pnl": pnl, "sts": status, "rec": recent, "ref": refresh_menu, "acc": account_picker, "ac": account_card}

# slash command -> (view, default arg)
COMMAND_VIEWS = {"start": ("home", None), "menu": ("home", None), "status": ("sts", None), "today": ("home", None),
                 "positions": ("pos", None), "holdings": ("hold", None), "orders": ("ord", "all"), "pending": ("ord", "work"),
                 "rejected": ("ord", "rej"), "trades": ("trd", None), "pnl": ("pnl", "today"), "recent": ("rec", None),
                 "account": ("ac", None)}


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
        row.append(btn("👤 Account", "ac", e["account_id"]))
    row.append(btn("⚡ Desk", "home"))
    return kb(row)

