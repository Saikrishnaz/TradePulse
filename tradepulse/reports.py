"""Human-readable summaries (Telegram HTML) built from scoped data. Used by bot commands, the
scheduled group digests and position reports."""

from __future__ import annotations

from .config import now_ist, today_ist
from .notify.format import broker_name, h, hhmmss, inr, num
from .localdata import mask_account
from .service import DataService, Scope


def _acct_line(a: dict) -> str:
    return f"<b>{h(a.get('client_name') or '-')}</b> · {h(broker_name(a.get('broker')))} <code>{h(mask_account(a['account_id']))}</code>"


def status_text(svc: DataService, scope: Scope) -> str:
    accts = svc.accounts(scope)
    if not accts:
        return "No accounts are available to you."
    counts = svc.oms.order_counts(scope.account_ids, today_ist())
    pnl = svc.oms.pnl(scope.account_ids, since_day=today_ist(), ltp=svc.ltp)
    lines = [f"📡 <b>Account status</b> · {h(now_ist().strftime('%d %b %H:%M IST'))}", ""]
    for a in accts:
        c = counts.get(a["account_id"], {})
        p = pnl["accounts"].get(a["account_id"], {})
        worker = a.get("worker_status") or "-"
        session = a.get("session_status") or "-"
        mon = "🟢 monitored" if a["monitored"] else "⚪ not monitored"
        lines.append(_acct_line(a))
        lines.append(f"  {mon} · session {h(session)} · worker {h(worker)} · mode {h(a.get('trading_mode') or '-')}")
        lines.append(f"  Orders {c.get('total', 0)} (✅{c.get('filled', 0)} ❌{c.get('rejected', 0)} "
                     f"⏳{c.get('pending', 0)}) · P&L {inr(p.get('net', 0.0))} · open legs {p.get('open_legs', 0)}")
    return "\n".join(lines)


def today_text(svc: DataService, scope: Scope, title: str = "Today's trading") -> str:
    day = today_ist()
    counts = svc.oms.order_counts(scope.account_ids, day)
    fills = svc.oms.fills(scope.account_ids, day, limit=10000)
    pnl = svc.oms.pnl(scope.account_ids, since_day=day, ltp=svc.ltp)
    t = counts["_all"]
    tot = pnl["total"]
    lines = [f"📅 <b>{h(title)}</b> · {h(day)}", "",
             f"Orders: <b>{t['total']}</b> · filled {t['filled']} · partial {t['partial']} · pending {t['pending']} "
             f"· rejected {t['rejected']} · cancelled {t['cancelled']}",
             f"Trades executed: <b>{len(fills)}</b>",
             f"P&L: realised {inr(tot['realized'])} · unrealised {inr(tot['unrealized'])} · <b>net {inr(tot['net'])}</b>"]
    if tot.get("unpriced_open_legs"):
        lines.append(f"<i>{tot['unpriced_open_legs']} open leg(s) without a live price - unrealised P&L excludes them.</i>")
    per = [(aid, b) for aid, b in pnl["accounts"].items()]
    if len(scope.account_ids) > 1:
        lines += ["", "<b>By account</b>"]
        names = {a["account_id"]: a for a in svc.oms.accounts(scope.account_ids)}
        for aid in scope.account_ids:
            c = counts.get(aid)
            b = dict(per).get(aid)
            if not c and not b:
                continue
            a = names.get(aid, {"account_id": aid})
            lines.append(f"{_acct_line(a)}: orders {(c or {}).get('total', 0)} · net {inr((b or {}).get('net', 0.0))}")
    return "\n".join(lines)


def _baseline_note(svc: DataService, ids: list[str], kind: str) -> str:
    base = svc.oms.baselines(ids)
    ats = [b[kind]["at"] for b in base.values() if kind in b]
    if not ats:
        return "\n<i>No broker snapshot yet - built from today's fills only.</i>"
    return f"\n<i>Broker snapshot {h(hhmmss(min(ats)))} + live order updates since.</i>"


def holdings_text(svc: DataService, scope: Scope, account_ids: list[str] | None = None) -> str:
    ids = [a for a in (account_ids or scope.account_ids) if scope.allows(a)]
    rows = svc.oms.holdings(ids, ltp=svc.ltp)
    if not rows:
        return "🏦 No holdings recorded." + _baseline_note(svc, ids, "holdings")
    lines = [f"🏦 <b>Holdings</b> ({len(rows)})", ""]
    for r in rows[:40]:
        extra = []
        if r["t1_quantity"]:
            extra.append(f"T1 {r['t1_quantity']}")
        if r["pledged_quantity"]:
            extra.append(f"pledged {r['pledged_quantity']}")
        if r["change"]:
            extra.append(f"today {'+' if r['change'] > 0 else ''}{r['change']}")
        lines.append(f"<b>{h(r['symbol'])}</b> {r['quantity']} @ {num(r['average_price'])} · LTP {num(r.get('ltp'))}"
                     f" · P&L {inr(r['pnl']) if r.get('pnl') is not None else 'n/a'}" + (f" · {', '.join(extra)}" if extra else ""))
        lines.append(f"  {h(r.get('client_name'))} · {h(broker_name(r.get('broker')))} <code>{h(mask_account(r['account_id']))}</code>")
    if len(rows) > 40:
        lines.append(f"… and {len(rows) - 40} more")
    lines.append(_baseline_note(svc, ids, "holdings"))
    return "\n".join(lines)


def positions_text(svc: DataService, scope: Scope, account_ids: list[str] | None = None) -> str:
    ids = [a for a in (account_ids or scope.account_ids) if scope.allows(a)]
    pos = svc.oms.positions(ids, "open", ltp=svc.ltp)
    if not pos:
        return "📊 No open positions."
    lines = [f"📊 <b>Open positions</b> ({len(pos)})", ""]
    for p in pos[:40]:
        u = p.get("unrealized_pnl")
        lines.append(f"<b>{h(p['symbol'])}</b> {h(p['product'])} {h(p['direction'])} {p['open_quantity']} @ {num(p['average_entry_price'])}"
                     f" · LTP {num(p.get('ltp'))} · P&L {inr(u) if u is not None else 'n/a'}")
        lines.append(f"  {h(p.get('client_name'))} · {h(broker_name(p.get('broker')))} "
                     f"<code>{h(mask_account(p['account_id']))}</code>" + (f" · {h(p.get('trading_mode'))}" if p.get("trading_mode") else ""))
    if len(pos) > 40:
        lines.append(f"… and {len(pos) - 40} more")
    lines.append(_baseline_note(svc, ids, "positions"))
    return "\n".join(lines)


def orders_text(svc: DataService, scope: Scope, account_ids: list[str] | None = None, status: str | None = None,
                day: str | None = None, limit: int = 15) -> str:
    ids = [a for a in (account_ids or scope.account_ids) if scope.allows(a)]
    rows = svc.oms.orders(ids, day=day or today_ist(), status=status, limit=limit)
    if not rows:
        return "🧾 No matching orders today."
    lines = [f"🧾 <b>Orders</b>{' · ' + h(status) if status else ''} (latest {len(rows)})", ""]
    for o in rows:
        lines.append(f"{h(o['created_at'][11:19])} <b>{h(o['side'])}</b> {h(o['symbol'])} "
                     f"{o['filled_quantity']}/{o['ordered_quantity']} · <b>{h(o['status'])}</b>"
                     f" · {h(o['client_name'])} <code>{h(mask_account(o['account_id']))}</code>")
        if o["status"] in ("REJECTED", "FAILED") and o.get("status_message"):
            lines.append(f"  ↳ {h(o['status_message'][:120])}")
    return "\n".join(lines)


def order_text(svc: DataService, scope: Scope, ref: str) -> str:
    o = svc.oms.order(scope.account_ids, ref.strip())
    if not o:
        return f"No order <code>{h(ref)}</code> found in your accounts."
    lines = [f"🧾 <b>Order {h(o['broker_order_id'] or o['internal_order_id'])}</b>", "",
             f"{h(o['client_name'])} · {h(broker_name(o['broker']))} <code>{h(mask_account(o['account_id']))}</code>",
             f"<b>{h(o['side'])}</b> {h(o['symbol'])} ({h(o['exchange'])}) · {h(o['order_type'])} {h(o['product'])}",
             f"Qty {o['filled_quantity']}/{o['ordered_quantity']} · price {num(o['price'])} · avg {num(o['average_price'])}",
             f"Status: <b>{h(o['status'])}</b>"]
    if o.get("status_message"):
        lines.append(f"Message: {h(o['status_message'][:300])}")
    if o["lifecycle"]:
        lines += ["", "<b>Lifecycle</b>"]
        for e in o["lifecycle"][-12:]:
            lines.append(f"{h(hhmmss(e['at']))} {h(e['from_status'] or '∅')} → <b>{h(e['to_status'])}</b> "
                         f"({h(e['source'])}) {h((e['message'] or '')[:80])}")
    if o["fills"]:
        lines += ["", "<b>Fills</b>"] + [f"{f['quantity']} @ {num(f['price'])} · {h(hhmmss(f['filled_at']))}" for f in o["fills"]]
    return "\n".join(lines)


def trades_text(svc: DataService, scope: Scope, account_ids: list[str] | None = None) -> str:
    rows = svc.oms.fills([a for a in (account_ids or scope.account_ids) if scope.allows(a)], day=today_ist(), limit=25)
    if not rows:
        return "💹 No trades executed today."
    lines = [f"💹 <b>Trades today</b> ({len(rows)})", ""]
    for f in rows:
        lines.append(f"{h(f['filled_at'][11:19])} <b>{h(f['side'])}</b> {h(f['symbol'])} {f['quantity']} @ "
                     f"{f['price']:,.2f} · {h(f['client_name'])} <code>{h(mask_account(f['account_id']))}</code>")
    return "\n".join(lines)


def recent_text(svc: DataService, scope: Scope, account_ids: list[str] | None = None, limit: int = 15) -> str:
    rows = []
    for aid in (account_ids or [None]):
        rows += svc.events(scope, account_id=aid, limit=limit)
    rows = sorted(rows, key=lambda r: r["id"], reverse=True)[:limit]
    if not rows:
        return "No recent activity."
    lines = [f"🕒 <b>Recent activity</b>", ""]
    for r in rows:
        lines.append(f"{h(hhmmss(r['occurred_at']))} · {h(r['type'].replace('_', ' ').title())} · {h(r['symbol'])} "
                     f"{h(r['side'])} {r['filled_quantity'] or r['quantity'] or ''} · {h(r['client_name'])}")
    return "\n".join(lines)


def pnl_text(svc: DataService, scope: Scope, period: str = "today", account_ids: list[str] | None = None) -> str:
    sub = Scope([a for a in (account_ids or scope.account_ids) if scope.allows(a)], scope.label)
    perf = svc.performance(sub, period)
    t = perf["total"]
    lines = [f"💰 <b>P&L · {h(period)}</b> (since {h(perf['since'])})", "",
             f"Realised {inr(t['realized'])} · Unrealised {inr(t['unrealized'])} · <b>Net {inr(t['net'])}</b>",
             f"Closed {t['closed_legs']} · open {t['open_legs']} · win rate {t['win_rate'] if t['win_rate'] is not None else '-'}%"]
    if len(perf["accounts"]) > 1:
        lines.append("")
        for r in sorted(perf["accounts"], key=lambda r: r["net"], reverse=True):
            lines.append(f"{h(r['client_name'])} <code>{h(r['masked_account'])}</code>: <b>{inr(r['net'])}</b>"
                         f" · orders {r['orders']['total']}")
    return "\n".join(lines)


def accounts_text(svc: DataService, scope: Scope) -> str:
    accts = svc.accounts(scope)
    if not accts:
        return "No accounts are available to you."
    lines = [f"👥 <b>Your accounts</b> ({len(accts)})", ""]
    for a in accts:
        lines.append(f"{'🟢' if a['monitored'] else '⚪'} {_acct_line(a)}"
                     + (f" · groups: {h(', '.join(g['name'] for g in a['groups']))}" if a["groups"] else ""))
    return "\n".join(lines)


def groups_text(groups: list[dict]) -> str:
    if not groups:
        return "No monitoring groups available to you."
    lines = ["🗂 <b>Monitoring groups</b>", ""]
    for g in groups:
        lines.append(f"{'🟢' if g['enabled'] else '⏸'} <b>{h(g['name'])}</b> · {len(g['account_ids'])} account(s) · "
                     f"{h(g['delivery_mode'])}")
    return "\n".join(lines)


def digest_text(svc: DataService, group: dict, kind: str = "digest") -> str:
    scope = Scope(list(group["account_ids"]), f"group: {group['name']}")
    if kind == "positions":
        return f"🗂 <b>{h(group['name'])}</b>\n\n" + positions_text(svc, scope)
    title = "End-of-day summary" if kind == "eod" else "Trading summary"
    return (f"🗂 <b>{h(group['name'])} – {h(title)}</b>\n\n" + today_text(svc, scope, title)
            + "\n\n" + positions_text(svc, scope))
