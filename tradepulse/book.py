"""Position & holdings book: broker baseline + fills since, with exact Decimal arithmetic.

The book for one account is *derived*, never mutated in place:

    positions = latest positions snapshot (the baseline)  ⊕  every fill with filled_at > baseline time
    holdings  = latest holdings snapshot  (the baseline)  ⊕  every delivery (CNC/MTF) fill after it

Recomputing it on each fill makes it idempotent: a redelivered fill, a fill that arrives before or
after a snapshot, or a restart can never double-count. Keys are how the brokers and the OMS key a
position: (exchange, exchange_token, product).

Average-cost method: adding to a position re-weights the average; reducing it books
``closed x (exit - average)`` (sign-adjusted for shorts) and keeps the average; crossing zero closes
the old side and opens the remainder at the fill price.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

D0 = Decimal("0")
CENT = Decimal("0.01")
DELIVERY = ("CNC", "MTF")


def dec(v) -> Decimal:
    if v is None or v == "":
        return D0
    return Decimal(str(v))


def q2(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def pos_key(r: dict) -> tuple:
    return (str(r.get("exchange") or "").upper(), int(r.get("exchange_token") or 0), str(r.get("product") or "").upper())


# --- one position -----------------------------------------------------------------------------
def apply_fill(p: dict, side: str, qty: int, price) -> str:
    """Mutates position ``p`` (net_quantity, average_price, realized, day_buy_qty, day_sell_qty).
    Returns what happened: OPENED / INCREASED / REDUCED / CLOSED / FLIPPED."""
    qty = int(qty)
    price = dec(price)
    signed = qty if side.upper() == "BUY" else -qty
    net = int(p.get("net_quantity") or 0)
    avg = dec(p.get("average_price"))
    realized = dec(p.get("realized"))
    if side.upper() == "BUY":
        p["day_buy_qty"] = int(p.get("day_buy_qty") or 0) + qty
    else:
        p["day_sell_qty"] = int(p.get("day_sell_qty") or 0) + qty
    if net == 0 or (net > 0) == (signed > 0):
        new = net + signed
        avg = (abs(net) * avg + qty * price) / abs(new)
        action = "OPENED" if net == 0 else "INCREASED"
    else:
        closing = min(qty, abs(net))
        direction = 1 if net > 0 else -1
        realized += closing * (price - avg) * direction
        new = net + signed
        if new == 0:
            action, avg = "CLOSED", D0
        elif (new > 0) != (net > 0):
            action, avg = "FLIPPED", price           # the remainder opens at the fill price
        else:
            action = "REDUCED"
    p["net_quantity"] = new
    p["average_price"] = str(q2(avg) if avg else D0)
    p["realized"] = str(q2(realized))
    return action


def unrealized(p: dict, ltp) -> Decimal | None:
    net = int(p.get("net_quantity") or 0)
    if not net:
        return D0
    if ltp is None or float(ltp) <= 0:
        return None
    return q2(net * (dec(ltp) - dec(p.get("average_price"))))


# --- holdings -----------------------------------------------------------------------------------
def apply_delivery_fill(h: dict, side: str, qty: int, price) -> str:
    """A CNC/MTF fill changes the expected holding: a buy is T1 (not yet settled, not sellable); a
    sell comes out of sellable first. Returns BOUGHT / SOLD / SOLD_OUT / OVERSOLD."""
    qty = int(qty)
    quantity = int(h.get("quantity") or 0)
    if side.upper() == "BUY":
        avg = dec(h.get("average_price"))
        new = quantity + qty
        h["average_price"] = str(q2((quantity * avg + qty * dec(price)) / new)) if new else "0"
        h["quantity"] = new
        h["t1_quantity"] = int(h.get("t1_quantity") or 0) + qty
        return "BOUGHT"
    h["quantity"] = quantity - qty
    h["sellable"] = max(0, int(h.get("sellable") or 0) - qty)
    if h["quantity"] < 0:
        return "OVERSOLD"
    return "SOLD_OUT" if h["quantity"] == 0 else "SOLD"


# --- derive the whole book ----------------------------------------------------------------------
def derive_positions(baseline: list[dict], fills: list[dict]) -> tuple[dict, dict]:
    """(book keyed by pos_key, {key: last action}) from baseline rows + fills (already filtered/ordered)."""
    book: dict[tuple, dict] = {}
    for r in baseline:
        k = pos_key(r)
        book[k] = {"exchange": k[0], "exchange_token": k[1], "product": k[2],
                   "symbol": r.get("broker_symbol") or r.get("symbol") or "",
                   "instrument_class": r.get("instrument_class") or "",
                   "net_quantity": int(r.get("net_quantity") or 0),
                   "average_price": str(q2(dec(r.get("average_price")))),
                   "realized": "0", "day_buy_qty": 0, "day_sell_qty": 0,
                   "last_price": float(r.get("last_price") or 0), "broker_pnl": float(r.get("pnl") or 0),
                   "baseline_qty": int(r.get("net_quantity") or 0), "fills_applied": 0}
    actions: dict[tuple, str] = {}
    for f in fills:
        k = pos_key(f)
        p = book.setdefault(k, {"exchange": k[0], "exchange_token": k[1], "product": k[2],
                                "symbol": f.get("symbol") or "", "instrument_class": f.get("instrument_class") or "",
                                "net_quantity": 0, "average_price": "0", "realized": "0", "day_buy_qty": 0,
                                "day_sell_qty": 0, "last_price": 0.0, "broker_pnl": 0.0, "baseline_qty": 0,
                                "fills_applied": 0})
        if not p.get("symbol"):
            p["symbol"] = f.get("symbol") or ""
        actions[k] = apply_fill(p, f["side"], f["quantity"], f["price"])
        p["fills_applied"] += 1
    return book, actions


def derive_holdings(baseline: list[dict], fills: list[dict]) -> tuple[dict, dict]:
    book: dict[tuple, dict] = {}
    for r in baseline:
        k = pos_key({**r, "product": r.get("product") or "CNC"})
        book[k] = {"exchange": k[0], "exchange_token": k[1], "product": k[2],
                   "symbol": r.get("broker_symbol") or r.get("symbol") or "", "isin": r.get("isin") or "",
                   "quantity": int(r.get("quantity") or 0), "sellable": int(r.get("sellable") or 0),
                   "t1_quantity": int(r.get("t1_quantity") or 0), "pledged_quantity": int(r.get("pledged_quantity") or 0),
                   "average_price": str(q2(dec(r.get("average_price")))), "last_price": float(r.get("last_price") or 0),
                   "baseline_qty": int(r.get("quantity") or 0)}
    actions: dict[tuple, str] = {}
    for f in fills:
        if str(f.get("product") or "").upper() not in DELIVERY:
            continue
        # A holding is product-agnostic at most brokers (CNC); MTF stays MTF.
        k = pos_key(f)
        h = book.get(k) or book.get((k[0], k[1], "CNC")) or book.setdefault(
            k, {"exchange": k[0], "exchange_token": k[1], "product": k[2], "symbol": f.get("symbol") or "", "isin": "",
                "quantity": 0, "sellable": 0, "t1_quantity": 0, "pledged_quantity": 0, "average_price": "0",
                "last_price": 0.0, "baseline_qty": 0})
        actions[pos_key(h)] = apply_delivery_fill(h, f["side"], f["quantity"], f["price"])
    return book, actions


def compare(book: dict, snapshot: list[dict], qty_field: str, default_product: str = "") -> list[dict]:
    """Quantity differences between the book and a broker snapshot. Absent = 0 on either side."""
    snap = {}
    for r in snapshot:
        k = pos_key({**r, "product": r.get("product") or default_product})
        snap[k] = snap.get(k, 0) + int(r.get(qty_field) or 0)
    out = []
    for k in sorted(set(book) | set(snap)):
        b = int((book.get(k) or {}).get(qty_field) or 0)
        s = snap.get(k, 0)
        if b != s:
            sym = (book.get(k) or {}).get("symbol") or next((r.get("broker_symbol") or r.get("symbol") or ""
                                                             for r in snapshot if pos_key({**r, "product": r.get("product") or default_product}) == k), "")
            out.append({"exchange": k[0], "exchange_token": k[1], "product": k[2], "symbol": sym,
                        "book_qty": b, "broker_qty": s})
    return out
