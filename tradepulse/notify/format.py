"""Telegram message text (HTML parse mode). Every dynamic value is escaped; account ids are masked."""

from __future__ import annotations

from datetime import datetime
from html import escape

from ..events import (ACCOUNT_ALERT, EVENT_LABELS, ORDER_FILLED, ORDER_REJECTED, POSITION_UPDATED,
                      TRADE_EXECUTED)
from ..localdata import mask_account

MAX_TEXT = 3900
BROKERS = {"zerodha": "Zerodha", "angel": "Angel One", "5paisa": "5paisa", "fivepaisa": "5paisa", "paper": "Paper"}

ICON = {"ORDER_PLACED": "🆕", "ORDER_OPEN": "📬", "ORDER_MODIFIED": "✏️", "ORDER_PARTIALLY_FILLED": "◐",
        "ORDER_FILLED": "✅", "TRADE_EXECUTED": "💹", "ORDER_CANCELLED": "🚫", "ORDER_REJECTED": "❌",
        "ORDER_UNKNOWN": "⚠️", "POSITION_UPDATED": "📊", "ACCOUNT_ALERT": "🚨"}


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


def event_text(e: dict) -> str:
    et = e["type"]
    title = EVENT_LABELS.get(et, et).upper()
    mode = (e.get("trading_mode") or "").upper()
    tag = f" · {mode}" if mode and mode != "LIVE" else ""
    lines = [f"{ICON.get(et, '•')} <b>TRADEPULSE – {h(title)}</b>{h(tag)}", ""]
    lines += [f"Client: <b>{h(e.get('client_name') or '-')}</b>",
              f"Broker: {h(broker_name(e.get('broker')))}",
              f"Account: <code>{h(mask_account(e.get('account_id')))}</code>"]
    if et == ACCOUNT_ALERT:
        sev = (e.get("payload") or {}).get("severity") or ""
        kind = (e.get("payload") or {}).get("kind") or ""
        lines += ["", f"Alert: <b>{h(kind)}</b> {h(sev)}"]
        if e.get("symbol"):
            lines.append(f"Symbol: {h(e['symbol'])} {h(e.get('side') or '')}")
        lines.append(h(e.get("message") or ""))
    elif et == POSITION_UPDATED:
        p = e.get("payload") or {}
        lines += ["", f"Symbol: <b>{h(e.get('symbol'))}</b>" + (f" ({h(e['exchange'])})" if e.get("exchange") else ""),
                  f"Position: <b>{h(e.get('status'))}</b> · {h(e.get('side'))}",
                  f"Open qty: {h(e.get('quantity'))} (was {h(p.get('previous_open', 0))})",
                  f"Avg entry: {num(e.get('average_price'))}"]
        if e.get("price"):
            lines.append(f"Avg exit: {num(e.get('price'))}")
    else:
        qty = e.get("quantity") or 0
        lines += ["", f"Symbol: <b>{h(e.get('symbol'))}</b>",
                  f"Exchange: {h(e.get('exchange') or '-')}",
                  f"Side: <b>{h(e.get('side') or '-')}</b>" + (f" · {h(e.get('order_type'))}" if e.get("order_type") else "")]
        if et == TRADE_EXECUTED:
            lines += [f"Quantity: {h(qty)}", f"Price: {num(e.get('price'))}"]
        else:
            lines.append(f"Quantity: {h(e.get('filled_quantity') or 0)}/{h(qty)}" if et != "ORDER_PLACED" else f"Quantity: {h(qty)}")
            if et in (ORDER_FILLED, "ORDER_PARTIALLY_FILLED") and e.get("average_price"):
                lines.append(f"Avg price: {num(e.get('average_price'))}")
            elif e.get("price"):
                lines.append(f"Price: {num(e.get('price'))}")
        lines.append(f"Status: <b>{h(e.get('status') or '-')}</b>")
        if e.get("order_id"):
            lines.append(f"Order ID: <code>{h(e['order_id'])}</code>")
        if et == ORDER_REJECTED and e.get("message"):
            lines += ["", f"Reason: {h(e['message'][:300])}"]
    lines += ["", f"Time: {h(hhmmss(e.get('occurred_at')))}"]
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
