"""Book math: exact Decimal expectations for every position transition, holdings and comparison."""

from decimal import Decimal as D

from tradepulse import book as bk


def pos(net=0, avg="0"):
    return {"net_quantity": net, "average_price": avg, "realized": "0", "day_buy_qty": 0, "day_sell_qty": 0}


def test_open_increase_reduce_close_long():
    p = pos()
    assert bk.apply_fill(p, "BUY", 75, "100.10") == "OPENED"
    assert bk.apply_fill(p, "BUY", 75, "101.30") == "INCREASED"
    assert p["net_quantity"] == 150 and D(p["average_price"]) == D("100.70")
    assert bk.apply_fill(p, "SELL", 50, "102.70") == "REDUCED"
    assert D(p["realized"]) == D("100.00")                  # 50 x 2.00
    assert D(p["average_price"]) == D("100.70")             # average unchanged on reduce
    assert bk.apply_fill(p, "SELL", 100, "99.70") == "CLOSED"
    assert p["net_quantity"] == 0 and D(p["realized"]) == D("0.00")   # +100 then -100
    assert p["day_buy_qty"] == 150 and p["day_sell_qty"] == 150


def test_short_and_flip():
    p = pos()
    assert bk.apply_fill(p, "SELL", 35, "410.00") == "OPENED"
    assert p["net_quantity"] == -35
    assert bk.apply_fill(p, "BUY", 70, "395.35") == "FLIPPED"
    assert D(p["realized"]) == D("512.75")                  # 35 x 14.65 short gain
    assert p["net_quantity"] == 35 and D(p["average_price"]) == D("395.35")


def test_baseline_plus_fills_and_unrealized():
    base = [{"exchange": "NFO", "exchange_token": 56723, "product": "NRML", "net_quantity": 75, "average_price": 120.0,
             "broker_symbol": "NIFTY26NOV26000CE", "last_price": 125.0}]
    fills = [{"exchange": "NFO", "exchange_token": 56723, "product": "NRML", "side": "SELL", "quantity": 25, "price": 130.0,
              "symbol": "NIFTY26NOV26000CE"}]
    book, actions = bk.derive_positions(base, fills)
    p = book[("NFO", 56723, "NRML")]
    assert p["net_quantity"] == 50 and p["baseline_qty"] == 75 and actions[("NFO", 56723, "NRML")] == "REDUCED"
    assert D(p["realized"]) == D("250.00")
    assert bk.unrealized(p, 128.10) == D("405.00")          # 50 x 8.10
    assert bk.unrealized(p, None) is None


def test_holdings_delivery_fills():
    base = [{"exchange": "NSE", "exchange_token": 2885, "product": "CNC", "broker_symbol": "RELIANCE-EQ", "quantity": 10,
             "sellable": 10, "average_price": 1300.0}]
    fills = [{"exchange": "NSE", "exchange_token": 2885, "product": "CNC", "side": "BUY", "quantity": 5, "price": 1312.0},
             {"exchange": "NSE", "exchange_token": 2885, "product": "CNC", "side": "SELL", "quantity": 3, "price": 1320.0},
             {"exchange": "NFO", "exchange_token": 1, "product": "NRML", "side": "BUY", "quantity": 75, "price": 1.0}]
    book, actions = bk.derive_holdings(base, fills)
    h = book[("NSE", 2885, "CNC")]
    assert h["quantity"] == 12 and h["t1_quantity"] == 5 and h["sellable"] == 7
    assert D(h["average_price"]) == D("1304.00")            # (10x1300 + 5x1312) / 15
    assert ("NFO", 1, "NRML") not in book                    # F&O never touches holdings


def test_compare_absent_is_zero():
    book = {("NSE", 1, "CNC"): {"net_quantity": 5, "symbol": "A"}, ("NSE", 2, "MIS"): {"net_quantity": 0, "symbol": "B"}}
    snap = [{"exchange": "NSE", "exchange_token": 1, "product": "CNC", "net_quantity": 5},
            {"exchange": "NSE", "exchange_token": 3, "product": "MIS", "net_quantity": -10, "broker_symbol": "C"}]
    diffs = bk.compare(book, snap, "net_quantity")
    assert diffs == [{"exchange": "NSE", "exchange_token": 3, "product": "MIS", "symbol": "C", "book_qty": 0, "broker_qty": -10}]
