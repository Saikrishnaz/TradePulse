"""Telegram message text (HTML parse mode). Every dynamic value is escaped; account ids are masked."""

from __future__ import annotations

from datetime import datetime
from html import escape

from ..events import (ACCOUNT_ALERT, EVENT_LABELS, ORDER_FILLED, ORDER_REJECTED, POSITION_UPDATED,
                      TRADE_EXECUTED)

MAX_TEXT = 3900
BROKERS = {"zerodha": "Zerodha", "angel": "Angel One", "5paisa": "5paisa", "fivepaisa": "5paisa", "paper": "Paper"}

ICON = {"ORDER_PLACED": "🆕", "ORDER_OPEN": "📬", "ORDER_MODIFIED": "✏️", "ORDER_PARTIALLY_FILLED": "◐",
        "ORDER_FILLED": "✅", "TRADE_EXECUTED": "💹", "ORDER_CANCELLED": "🚫", "ORDER_REJECTED": "❌",
        "ORDER_UNKNOWN": "⚠️", "POSITION_UPDATED": "📊", "HOLDING_UPDATED": "🏦", "RECONCILE_MISMATCH": "⚠️",
        "ACCOUNT_ALERT": "🚨"}


def show_id(account_id) -> str:
    """Telegram shows the full client id so readers know exactly which account it is."""
    return str(account_id or "")


def h(v) -> str:
    return escape(str(v if v is not None else ""), quote=False)


def broker_name(b: str) -> str:
    return BROKERS.get(str(b or "").lower(), str(b or "-").title())


def num(v, dp: int = 2) -> str:
    if v is None or v == "":
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:,.{dp}f}"


def inr(v) -> str:
    if v is None:
        return "-"
    f = float(v)
    sign = "+" if f > 0 else "−" if f < 0 else ""
    return f"{sign}₹{abs(f):,.2f}"


def hhmmss(iso: str) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%H:%M:%S IST")
    except (TypeError, ValueError):
        return str(iso or "")


MONTHS = {m: m.title() for m in ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")}
_MON = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"


def pretty_symbol(sym: str) -> str:
    """NIFTY23NOV2625400CE -> 'NIFTY 25400 CE · 23 Nov';  NIFTY26NOV26000CE -> 'NIFTY 26000 CE · Nov';
    NIFTY26NOVFUT -> 'NIFTY FUT · Nov';  RELIANCE-EQ -> 'RELIANCE'. Unknown shapes are returned unchanged."""
    import re
    s = str(sym or "").upper()
    strike = r"([1-9]\d{1,6}(?:\.\d+)?)"           # a strike never starts with 0 - this separates the formats
    # Angel: NAME DD MON YY STRIKE (NIFTY23NOV2625400CE). Shares a shape with Zerodha's NAME YY MON STRIKE
    # (NIFTY26NOV25800PE could read 26-Nov-25 strike 800): accept the Angel reading only when that expiry is
    # a real, not-long-past date - a live contract never expired over a week ago.
    m = re.fullmatch(rf"([A-Z&]+?)(\d{{2}})({_MON})(\d{{2}}){strike}(CE|PE)", s)
    if m:
        from datetime import date, timedelta
        try:
            expiry = date(2000 + int(m[4]), list(MONTHS).index(m[3]) + 1, int(m[2]))
            if expiry >= date.today() - timedelta(days=7):
                return f"{m[1]} {m[5]} {m[6]} · {int(m[2])} {MONTHS[m[3]]}"
        except ValueError:
            pass
    # Zerodha monthly: NAME YY MON STRIKE (NIFTY26NOV26000CE)
    m = re.fullmatch(rf"([A-Z&]+?)(\d{{2}})({_MON}){strike}(CE|PE)", s)
    if m:
        return f"{m[1]} {m[4]} {m[5]} · {MONTHS[m[3]]}"
    # Zerodha weekly: NAME YY M DD STRIKE, month 1-9 / O / N / D (NIFTY2611726000CE)
    m = re.fullmatch(rf"([A-Z&]+?)(\d{{2}})([1-9OND])(\d{{2}}){strike}(CE|PE)", s)
    if m:
        mon = list(MONTHS.values())[("123456789OND".index(m[3]))]
        return f"{m[1]} {m[5]} {m[6]} · {int(m[4])} {mon}"
    m = re.fullmatch(rf"([A-Z&]+?)(\d{{2}})({_MON})(\d{{2}})?FUT", s)
    if m:
        return f"{m[1]} FUT · {MONTHS[m[3]]}"
    for suffix in ("-EQ", "-BE", "-SM"):
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s or "–"


def _side(side: str, qty) -> str:
    s = str(side or "").upper()
    dot = "🟢" if s in ("BUY", "LONG") else "🔴" if s in ("SELL", "SHORT") else "⚪"
    return f"{dot} <b>{h(s)} {int(qty or 0):,}</b>"


TITLES = {"ORDER_PLACED": "Order placed", "ORDER_OPEN": "Order open", "ORDER_MODIFIED": "Order modified",
          "ORDER_PARTIALLY_FILLED": "Partially filled", "ORDER_FILLED": "Order filled", "TRADE_EXECUTED": "Trade executed",
          "ORDER_CANCELLED": "Order cancelled", "ORDER_REJECTED": "Order rejected", "ORDER_UNKNOWN": "Order status unknown",
          "POSITION_UPDATED": "Position", "HOLDING_UPDATED": "Holding", "RECONCILE_MISMATCH": "Reconcile mismatch",
          "ACCOUNT_ALERT": "Alert"}


def event_text(e: dict) -> str:
    """An alert card: status header · who · what · detail · when. Buttons are attached by the notifier."""
    et = e["type"]
    p = e.get("payload") or {}
    mode = (e.get("trading_mode") or "").upper()
    title = TITLES.get(et, EVENT_LABELS.get(et, et))
    if et in ("POSITION_UPDATED", "HOLDING_UPDATED"):
        title = f"{title} {str(e.get('status') or '').lower()}".strip()
    head = f"{ICON.get(et, '•')} <b>{h(title)}</b> · {h(broker_name(e.get('broker')))}"
    if mode and mode != "LIVE":
        head += f" · 🧪 {h(mode)}"
    lines = [head, f"<b>{h(e.get('client_name') or '–')}</b>  <code>{h(show_id(e.get('account_id')))}</code>", ""]
    sym = h(pretty_symbol(e.get("symbol")))
    if et == ACCOUNT_ALERT:
        critical = str(p.get("severity") or "").upper() == "CRITICAL"
        kind = {"WORKER_ALERT": "Order worker"}.get(p.get("kind"), str(p.get("kind") or "Alert").replace("_", " ").title())
        lines.append(f"{'🚨' if critical else '⚠️'} <b>{h(kind)} {'error' if critical else 'warning'}</b>")
        if e.get("symbol"):
            qty = int(e.get("quantity") or 0)
            lines.append(f"{_side(e.get('side'), qty)} × <b>{sym}</b>"
                         + (f" · filled <b>{int(e.get('filled_quantity') or 0):,}/{qty:,}</b>" if qty else ""))
        if e.get("message"):
            lines.append(f"<blockquote>{h(e['message'][:400])}</blockquote>")
    elif et == "RECONCILE_MISMATCH":
        lines += [f"<b>{sym}</b> · {h(e.get('exchange'))} {h(e.get('product'))}",
                  f"Book <b>{p.get('book_qty')}</b> → Broker <b>{p.get('broker_qty')}</b>  <i>({h(p.get('kind') or '')})</i>"]
    elif et in (POSITION_UPDATED, "HOLDING_UPDATED"):
        prev, cur = p.get("previous", p.get("previous_open", 0)), e.get("quantity")
        lines += [f"<b>{sym}</b> · {h(e.get('exchange') or '')} {h(e.get('product') or '')}".rstrip(),
                  f"Qty <b>{prev} → {cur}</b>" + (f" · avg {num(e.get('average_price'))}" if e.get("average_price") else "")]
        if e.get("price"):
            lines.append(f"Last fill @ {num(e.get('price'))}")
    else:
        qty = e.get("quantity") or 0
        lines.append(f"{_side(e.get('side'), qty)} × <b>{sym}</b>")
        meta = " · ".join(x for x in (e.get("exchange"), e.get("order_type"), e.get("product")) if x)
        if et == TRADE_EXECUTED:
            lines.append(f"@ <b>{num(e.get('price'))}</b>" + (f" · {h(meta)}" if meta else ""))
        elif et in (ORDER_FILLED, "ORDER_PARTIALLY_FILLED"):
            lines.append(f"Filled <b>{int(e.get('filled_quantity') or 0):,}/{int(qty):,}</b>"
                         + (f" @ <b>{num(e.get('average_price'))}</b>" if e.get("average_price") else "") + (f" · {h(meta)}" if meta else ""))
        else:
            lines.append(h(meta) + (f" · price {num(e.get('price'))}" if e.get("price") else ""))
        if et in (ORDER_REJECTED, "ORDER_UNKNOWN", "ORDER_CANCELLED") and e.get("message"):
            lines.append(f"<blockquote>{h(e['message'][:300])}</blockquote>")
    foot = h(hhmmss(e.get("occurred_at")))
    if e.get("order_id"):
        foot += f" · order <code>{h(e['order_id'])}</code>"
    lines += ["", f"<i>{foot}</i>"]
    return "\n".join(lines)[:MAX_TEXT]


def batch_text(texts: list[str]) -> list[str]:
    """Join queued messages into as few Telegram messages as fit (batched delivery mode)."""
    out, cur = [], ""
    sep = "\n\n━━━━━━━━━━\n\n"
    for t in texts:
        if cur and len(cur) + len(sep) + len(t) > MAX_TEXT:
            out.append(cur)
            cur = t
        else:
            cur = f"{cur}{sep}{t}" if cur else t
    if cur:
        out.append(cur)
    if len(out) > 0 and len(texts) > 1:
        out[0] = f"🗂 <b>TradePulse – {len(texts)} updates</b>\n\n" + out[0]
    return [o[:4096] for o in out]


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


FINAL_HEAD = {"ORDER_FILLED": ("✅", "Order filled"), "ORDER_CANCELLED": ("🚫", "Order cancelled"),
              "ORDER_REJECTED": ("❌", "Order rejected")}


def _secs(a: str, b: str) -> str:
    try:
        s = int((datetime.fromisoformat(str(b)) - datetime.fromisoformat(str(a))).total_seconds())
    except (TypeError, ValueError):
        return ""
    return f"{s}s" if s < 120 else f"{s // 60}m {s % 60}s" if s < 3600 else f"{s // 3600}h {s % 3600 // 60}m"


def order_final_text(store, event_id: str) -> str:
    """The ONE message for an order ("final" mode): how it ended plus everything that happened on the way -
    every execution, average price, modifications, worker alerts and the position it left."""
    e = store.one("SELECT * FROM events WHERE event_id = ?", (event_id,))
    if not e or not e.get("internal_order_id"):
        return ""
    o = store.one("SELECT * FROM orders WHERE internal_order_id = ?", (e["internal_order_id"],))
    if not o:
        return ""
    fills = store.rows("SELECT quantity, price, filled_at FROM fills WHERE internal_order_id = ? ORDER BY filled_at, id",
                       (o["internal_order_id"],))
    alerts = store.rows("""SELECT message FROM events WHERE internal_order_id = ? AND type = 'ACCOUNT_ALERT'
                           ORDER BY id""", (o["internal_order_id"],))
    mods = max(int(o.get("modification_count") or 0),
               (store.one("SELECT COUNT(*) AS n FROM events WHERE internal_order_id = ? AND type = 'ORDER_MODIFIED'",
                          (o["internal_order_id"],)) or {}).get("n") or 0)
    qty, filled = int(o["ordered_quantity"] or 0), int(o["filled_quantity"] or 0)
    if fills:
        filled = max(filled, sum(int(f["quantity"]) for f in fills))
    avg = float(o["average_price"] or 0)
    if fills and not avg:
        avg = sum(float(f["price"]) * int(f["quantity"]) for f in fills) / max(1, sum(int(f["quantity"]) for f in fills))
    icon, title = FINAL_HEAD.get(e["type"], ("•", e["type"].replace("_", " ").title()))
    if e["type"] == "ORDER_CANCELLED" and filled:
        icon, title = "◐", "Order cancelled · partly filled"
    mode = (o.get("trading_mode") or e.get("trading_mode") or "").upper()
    head = f"{icon} <b>{h(title)}</b> · {h(broker_name(o.get('broker') or e.get('broker')))}"
    if mode and mode != "LIVE":
        head += f" · 🧪 {h(mode)}"
    sym = h(pretty_symbol(o["symbol"]))
    lines = [head, f"<b>{h(e.get('client_name') or '–')}</b>  <code>{h(show_id(o['account_id']))}</code>", "",
             f"{_side(o['side'], qty)} × <b>{sym}</b>",
             f"Filled <b>{filled:,}/{qty:,}</b>" + (f" @ avg <b>{num(avg)}</b>" if filled and avg else "")
             + f" · {h(' · '.join(x for x in (o.get('exchange'), o.get('order_type'), o.get('product')) if x))}"
             + (f" · limit {num(o['price'])}" if o.get("price") else "")]
    if fills:
        prices = [float(f["price"]) for f in fills]
        rng = num(min(prices)) if min(prices) == max(prices) else f"{num(min(prices))} – {num(max(prices))}"
        lines.append(f"💹 {len(fills)} execution{'s' if len(fills) != 1 else ''} · {rng}")
        if len(fills) > 1:
            body = "\n".join(f"{h(str(f['filled_at'])[11:19])}  {int(f['quantity']):,} @ {num(f['price'])}" for f in fills[:40])
            if len(fills) > 40:
                body += f"\n… {len(fills) - 40} more"
            lines.append(f"<blockquote expandable>{body}</blockquote>")
    extra = []
    if mods:
        extra.append(f"✏️ modified {mods}×")
    if alerts:
        extra.append(f"⚠️ {len(alerts)} alert{'s' if len(alerts) != 1 else ''}")
    if extra:
        lines.append(" · ".join(extra))
    if e["type"] in ("ORDER_REJECTED", "ORDER_CANCELLED") and (o.get("status_message") or e.get("message")):
        lines.append(f"<blockquote>{h((o.get('status_message') or e.get('message'))[:300])}</blockquote>")
    seen = {(o.get("status_message") or "").strip()}
    notes = [a["message"] for a in alerts if a["message"] and a["message"].strip() not in seen]
    if notes:
        lines.append("<blockquote>" + "\n".join(f"⚠️ {h(m[:200])}" for m in notes[:5]) + "</blockquote>")
    pos = store.one("""SELECT net_quantity FROM book_positions WHERE account_id = ? AND exchange = ? AND exchange_token = ?
                       AND product = ?""", (o["account_id"], o.get("exchange") or "", int(o.get("exchange_token") or 0),
                                             o.get("product") or "")) if filled else None
    if pos is not None:
        n = int(pos["net_quantity"])
        lines.append(f"📊 Position now: <b>{'LONG ' + format(n, ',') if n > 0 else 'SHORT ' + format(-n, ',') if n < 0 else 'flat'}</b>")
    took = _secs(o.get("created_at"), e.get("occurred_at"))
    foot = f"{h(hhmmss(o.get('created_at')))[:8]} → {h(hhmmss(e.get('occurred_at')))}" + (f" ({took})" if took else "")
    if o.get("broker_order_id"):
        foot += f" · order <code>{h(o['broker_order_id'])}</code>"
    lines += ["", f"<i>{foot}</i>"]
    return "\n".join(lines)[:MAX_TEXT]
