"""The common event format every source is normalized into.

Dedupe keys are *semantic* where possible (account + broker order + lifecycle state + filled
quantity), so the same broker fact arriving from two sources - the broker's webhook AND the OMS
having applied it - produces one event and one notification, not two.
"""

from __future__ import annotations

import hashlib
import uuid

from .config import ist_iso

ORDER_PLACED = "ORDER_PLACED"
ORDER_MODIFIED = "ORDER_MODIFIED"
ORDER_CANCELLED = "ORDER_CANCELLED"
ORDER_REJECTED = "ORDER_REJECTED"
ORDER_OPEN = "ORDER_OPEN"
ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
ORDER_FILLED = "ORDER_FILLED"
ORDER_UNKNOWN = "ORDER_UNKNOWN"
TRADE_EXECUTED = "TRADE_EXECUTED"
POSITION_UPDATED = "POSITION_UPDATED"
HOLDING_UPDATED = "HOLDING_UPDATED"
RECONCILE_MISMATCH = "RECONCILE_MISMATCH"
ACCOUNT_ALERT = "ACCOUNT_ALERT"

EVENT_TYPES = [ORDER_PLACED, ORDER_OPEN, ORDER_MODIFIED, ORDER_PARTIALLY_FILLED, ORDER_FILLED, TRADE_EXECUTED,
               ORDER_CANCELLED, ORDER_REJECTED, ORDER_UNKNOWN, POSITION_UPDATED, HOLDING_UPDATED, RECONCILE_MISMATCH,
               ACCOUNT_ALERT]

EVENT_LABELS = {
    ORDER_PLACED: "Order placed", ORDER_OPEN: "Order open at broker", ORDER_MODIFIED: "Order modified",
    ORDER_PARTIALLY_FILLED: "Partially filled", ORDER_FILLED: "Order filled", TRADE_EXECUTED: "Trade executed",
    ORDER_CANCELLED: "Order cancelled", ORDER_REJECTED: "Order rejected", ORDER_UNKNOWN: "Order status unknown",
    POSITION_UPDATED: "Position update", HOLDING_UPDATED: "Holding update", RECONCILE_MISMATCH: "Reconcile mismatch",
    ACCOUNT_ALERT: "Account alert",
}

# Global defaults when no rule says otherwise. Lifecycle noise (open/modified) is off by default.
DEFAULT_ENABLED = {
    ORDER_PLACED: True, ORDER_OPEN: False, ORDER_MODIFIED: False, ORDER_PARTIALLY_FILLED: True,
    ORDER_FILLED: True, TRADE_EXECUTED: False, ORDER_CANCELLED: True, ORDER_REJECTED: True,
    ORDER_UNKNOWN: True, POSITION_UPDATED: True, HOLDING_UPDATED: True, RECONCILE_MISMATCH: True, ACCOUNT_ALERT: True,
}

# OMS order status -> event type. Statuses that precede broker submission are not separate events.
STATUS_EVENT = {
    "ROUTED": ORDER_PLACED, "OPEN": ORDER_OPEN, "PARTIALLY_FILLED": ORDER_PARTIALLY_FILLED,
    "COMPLETE": ORDER_FILLED, "REJECTED": ORDER_REJECTED, "FAILED": ORDER_REJECTED,
    "CANCELLED": ORDER_CANCELLED, "EXPIRED": ORDER_CANCELLED, "UNKNOWN": ORDER_UNKNOWN,
}


def new_event(type_: str, source: str, dedupe_key: str, **fields) -> dict:
    e = {
        "event_id": uuid.uuid4().hex, "type": type_, "source": source, "dedupe_key": dedupe_key,
        "account_id": "", "client_name": "", "broker": "", "trading_mode": "", "symbol": "", "exchange": "",
        "side": "", "order_type": "", "product": "", "quantity": 0, "filled_quantity": 0, "price": None,
        "average_price": None, "status": "", "order_id": "", "internal_order_id": "", "trade_id": "",
        "message": "", "payload": {}, "occurred_at": ist_iso(), "received_at": ist_iso(),
    }
    e.update({k: v for k, v in fields.items() if v is not None or k in ("price", "average_price")})
    e["broker"] = str(e["broker"] or "").lower()
    return e


def order_key(account_id: str, order_ref: str, event_type: str, filled: int | None = 0) -> str:
    return f"ord:{account_id}:{order_ref}:{event_type}:{int(filled or 0)}"


def stable_hash(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


# --- OMS rows -> events -------------------------------------------------------------------
def from_order_event(row: dict) -> dict | None:
    """One ``oms_order_events`` row (joined with its order) -> a normalized event, or None for noise."""
    frm, to = row.get("from_status") or "", row.get("to_status") or ""
    msg = row.get("message") or ""
    if frm == to:
        # Same-status rows are either a re-price (the OMS writes "re-priced to ...") or a poll echo.
        if to in ("OPEN", "PARTIALLY_FILLED") and msg.startswith("re-priced"):
            et = ORDER_MODIFIED
            key = f"oms-evt:{row['id']}"          # each re-price is its own fact
        else:
            return None
    else:
        et = STATUS_EVENT.get(to)
        if et is None:
            return None
        # Keyed on the broker order id once the broker has one, so a broker webhook for the same
        # transition (which only knows the broker id) dedupes against this OMS row.
        order_ref = row.get("broker_order_id") or row["internal_order_id"]
        key = order_key(row["account_id"], order_ref, et,
                        row.get("filled_quantity") if et in (ORDER_PARTIALLY_FILLED, ORDER_FILLED) else 0)
    return new_event(
        et, "OMS", key,
        account_id=row["account_id"], client_name=row.get("client_name") or "", broker=row.get("broker") or "",
        trading_mode=row.get("trading_mode") or "", symbol=row.get("symbol") or "", exchange=row.get("exchange") or "",
        side=row.get("side") or "", order_type=row.get("order_type") or "", product=row.get("product") or "",
        quantity=int(row.get("ordered_quantity") or 0), filled_quantity=int(row.get("filled_quantity") or 0),
        price=row.get("price"), average_price=row.get("average_price") or None, status=to,
        order_id=row.get("broker_order_id") or "", internal_order_id=row["internal_order_id"],
        trade_id=row.get("trade_id") or "", message=msg[:500],
        payload={"from_status": frm, "source": row.get("source"), "strategy_id": row.get("strategy_id")},
        occurred_at=row.get("at") or ist_iso())


def from_fill(row: dict) -> dict:
    return new_event(
        TRADE_EXECUTED, "OMS", f"fill:{row['account_id']}:{row['fill_id']}",
        account_id=row["account_id"], client_name=row.get("client_name") or "", broker=row.get("broker") or "",
        trading_mode=row.get("trading_mode") or "", symbol=row.get("symbol") or "", exchange=row.get("exchange") or "",
        side=row.get("side") or "", order_type=row.get("order_type") or "", product=row.get("product") or "",
        quantity=int(row.get("fill_quantity") or 0), filled_quantity=int(row.get("filled_quantity") or 0),
        price=row.get("fill_price"), average_price=row.get("average_price") or None, status="EXECUTED",
        order_id=row.get("broker_order_id") or "", internal_order_id=row.get("internal_order_id") or "",
        trade_id=row.get("exchange_trade_id") or row.get("fill_id") or "",
        payload={"fill_id": row.get("fill_id"), "fill_source": row.get("fill_source")},
        occurred_at=row.get("filled_at") or ist_iso())


def from_alert(row: dict) -> dict:
    return new_event(
        ACCOUNT_ALERT, "OMS", f"alert:{row['id']}",
        account_id=row.get("account_id") or "", client_name=row.get("client_name") or "",
        broker=row.get("broker") or "", trading_mode=row.get("trading_mode") or "", symbol=row.get("symbol") or "",
        side=row.get("side") or "", status=row.get("order_status") or "", internal_order_id=row.get("internal_order_id") or "",
        quantity=int(row.get("ordered_quantity") or 0), message=(row.get("message") or "")[:500],
        payload={"kind": row.get("kind"), "severity": row.get("severity")}, occurred_at=row.get("created_at") or ist_iso())


def from_leg_change(row: dict, previous: dict | None) -> dict | None:
    """A trade leg whose open quantity or status changed -> POSITION_UPDATED (opened/increased/reduced/closed)."""
    open_q = int(row.get("open_quantity") or 0)
    prev_q = int(previous["open_quantity"]) if previous else 0
    status = row.get("status") or ""
    if previous and prev_q == open_q and previous.get("status") == status:
        return None
    if not previous and open_q == 0 and int(row.get("filled_quantity") or 0) == 0:
        return None                                          # never filled: nothing to report
    if open_q > 0 and prev_q == 0:
        action = "OPENED"
    elif open_q == 0 and prev_q > 0:
        action = "CLOSED"
    elif open_q == 0 and not previous:
        action = "CLOSED"
    elif open_q > prev_q:
        action = "INCREASED"
    elif open_q < prev_q:
        action = "REDUCED"
    else:
        action = status
    return new_event(
        POSITION_UPDATED, "OMS", f"leg:{row['trade_leg_id']}:{open_q}:{status}",
        account_id=row["account_id"], client_name=row.get("client_name") or "", broker=row.get("broker") or "",
        trading_mode=row.get("trading_mode") or "", symbol=row.get("symbol") or "", exchange=row.get("exchange") or "",
        side=row.get("direction") or "", product=row.get("product") or "", quantity=open_q,
        filled_quantity=int(row.get("filled_quantity") or 0), price=row.get("average_exit_price") or None,
        average_price=row.get("average_entry_price") or None, status=action, trade_id=row.get("trade_id") or "",
        message=row.get("close_reason") or "",
        payload={"trade_leg_id": row["trade_leg_id"], "previous_open": prev_q, "closed_quantity": row.get("closed_quantity"),
                 "leg_status": status},
        occurred_at=row.get("updated_at") or ist_iso())
