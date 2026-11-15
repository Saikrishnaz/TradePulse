"""The next-day signal contract: the live webhook payload plus three fields.

    {"signal_id": "...", "strategy_id": "...",
     "trading_date": "2026-11-17",          # the session the orders are for
     "version": 1,                          # goes up with every resend of the same signal_id
     "execution": {"policy": "PREMARKET"},  # PREMARKET: placed 09:00-09:07 | OPEN: placed at 09:15
     "orders": [ ...exactly as on POST /api/v1/signals... ]}

Everything except those three fields is read by ``signals.parse_signal``, unchanged, so a LEG or
BASKET order means the same thing here as on the live webhook.

Only entries are taken: a next-day signal opens positions. What happens to a position after it is
open (exits, square-off, reconciliation) is the ordinary flow.

Pure functions, no database, no Redis and nothing from the OMS: TradePulse carries a byte-identical
copy (tradepulse/presignal/contract.py).
"""

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
STOP_TYPES = frozenset({"SL_LIMIT"})        # backend/oms/enums.STOP_TYPES
POLICIES = ("PREMARKET", "OPEN")
NEXT_DAY_FIELDS = ("trading_date", "version", "execution")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_VERSION = 1_000_000


class ContractError(Exception):
    def __init__(self, code: str, message: str, errors: list | None = None, http_status: int = 422):
        super().__init__(message)
        self.code, self.message, self.errors, self.http_status = code, message, errors or [], http_status


@dataclass(frozen=True)
class NextDay:
    trading_date: str
    version: int
    policy: str


@dataclass(frozen=True)
class Calendar:
    """When intake is open, and which weekdays the market is shut. Times are IST HH:MM."""
    intake_from: str = "15:30"      # on a trading day, signals for the next session from this time
    intake_until: str = "08:45"     # on the session's own morning, until this time (and the pickup lock)
    holidays: frozenset = frozenset()

    def to_dict(self) -> dict:
        return {"intake_from": self.intake_from, "intake_until": self.intake_until,
                "holidays": sorted(self.holidays)}

    @classmethod
    def from_dict(cls, d: dict) -> "Calendar":
        return cls(intake_from=str(d.get("intake_from") or "15:30"), intake_until=str(d.get("intake_until") or "08:45"),
                   holidays=frozenset(str(h) for h in (d.get("holidays") or [])))


def calendar_from_env() -> Calendar:
    holidays = frozenset(h.strip() for h in os.getenv("OMS_MARKET_HOLIDAYS", "").split(",") if DATE_RE.match(h.strip()))
    return Calendar(intake_from=os.getenv("OMS_PRESIGNAL_INTAKE_FROM", "15:30"),
                    intake_until=os.getenv("OMS_PRESIGNAL_INTAKE_UNTIL", "08:45"), holidays=holidays)


def is_trading_day(d: date, cal: Calendar) -> bool:
    return d.weekday() < 5 and d.isoformat() not in cal.holidays


def next_trading_day(d: date, cal: Calendar) -> date:
    for n in range(1, 15):
        nxt = d + timedelta(days=n)
        if is_trading_day(nxt, cal):
            return nxt
    raise ContractError("CALENDAR_INVALID", "no trading day in the next two weeks; check OMS_MARKET_HOLIDAYS", http_status=500)


def target_session(now: datetime, cal: Calendar) -> str | None:
    """The session a signal received ``now`` is for, or None while intake is shut (a trading day
    between the morning cutoff and the afternoon opening). Weekends and holidays are open all day."""
    local = now.astimezone(IST)
    today, hhmm = local.date(), local.strftime("%H:%M")
    if is_trading_day(today, cal):
        if hhmm < cal.intake_until:
            return today.isoformat()
        if hhmm < cal.intake_from:
            return None
    return next_trading_day(today, cal).isoformat()


def split(payload) -> tuple[dict, NextDay]:
    """(the payload as the live webhook reads it, the next-day fields). Raises ContractError."""
    if not isinstance(payload, dict):
        raise ContractError("INVALID_PAYLOAD", "Signal must be a JSON object", http_status=400)
    td = payload.get("trading_date")
    if not isinstance(td, str) or not DATE_RE.match(td):
        raise ContractError("INVALID_TRADING_DATE", "trading_date is required as YYYY-MM-DD")
    try:
        date.fromisoformat(td)
    except ValueError:
        raise ContractError("INVALID_TRADING_DATE", f"trading_date {td} is not a real date")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= MAX_VERSION:
        raise ContractError("INVALID_VERSION", "version is required: a whole number of 1 or more")
    execution = payload.get("execution")
    if not isinstance(execution, dict) or set(execution) - {"policy"}:
        raise ContractError("INVALID_EXECUTION_POLICY", 'execution must be {"policy": "PREMARKET" | "OPEN"}')
    policy = str(execution.get("policy") or "").strip().upper()
    if policy not in POLICIES:
        raise ContractError("INVALID_EXECUTION_POLICY", f"execution.policy must be one of {', '.join(POLICIES)}")
    base = {k: v for k, v in payload.items() if k not in NEXT_DAY_FIELDS}
    return base, NextDay(trading_date=td, version=version, policy=policy)


def check_session(nd: NextDay, now: datetime, cal: Calendar) -> None:
    """Refuse a signal sent outside the intake window, or for any session but the next one."""
    target = target_session(now, cal)
    if target is None:
        raise ContractError("INTAKE_CLOSED", f"next-day signals are taken from {cal.intake_from} until "
                                             f"{cal.intake_until} the next trading morning", http_status=409)
    if nd.trading_date != target:
        raise ContractError("INVALID_TRADING_DATE", f"trading_date must be the next session, {target}")


def policy_errors(orders, policy: str) -> list[dict]:
    """Order-level errors for the next-day rules. ``orders`` are parsed ``signals.Order`` objects after
    template defaults were applied (a BASKET order without order_type has the template's by then).
    ``price`` is the validated limit (tick-checked), passed per order as ``(order, price)``."""
    errors = []
    for o, price in orders:
        def bad(code, message, o=o):
            errors.append({"order_index": o.index, "leg_id": "" if o.leg_id == "BASKET" else o.leg_id,
                           "tag": o.tag, "code": code, "message": message})
        if o.effect != "OPEN":
            bad("NEXT_DAY_EXIT_NOT_SUPPORTED", f"{o.signal_side} closes a position; next-day signals only open "
                                               "positions - send exits to the live webhook during the session")
            continue
        if policy == "PREMARKET":
            if o.order_type in STOP_TYPES:
                bad("PREMARKET_NEEDS_LIMIT_PRICE", "a pre-market order is a plain LIMIT; SL_LIMIT entries go with "
                                                   "policy OPEN")
            elif o.order_type != "LIMIT" or price is None:
                bad("PREMARKET_NEEDS_LIMIT_PRICE", "a pre-market order must be order_type LIMIT with a price")
    return errors
