"""Redis keys of the next-day signal store. All under ``smx:presig:``.

Written by the OMS and by TradePulse (the one ``smx:`` prefix TradePulse may write), read by both.
TradePulse carries a byte-identical copy of this file (tradepulse/presignal/keys.py).
"""

PRESIG_PREFIX = "smx:presig:"
PRESIG_STRATEGIES = "smx:presig:strategies"     # JSON list of published strategy ids
PRESIG_CALENDAR = "smx:presig:calendar"         # JSON: intake hours + market holidays


def presig_strategy(strategy_id: str) -> str:
    """JSON template of one strategy (what intake validates against while the OMS is down)."""
    return f"smx:presig:strategy:{strategy_id}"


def presig_day(trading_date: str, strategy_id: str) -> str:
    """Hash signal_id -> current snapshot JSON: the signals waiting for that morning."""
    return f"smx:presig:day:{trading_date}:{strategy_id}"


def presig_versions(trading_date: str, strategy_id: str) -> str:
    """Hash '<signal_id>:<version>' -> snapshot JSON: every version, for audit and recovery."""
    return f"smx:presig:versions:{trading_date}:{strategy_id}"


def presig_tags(trading_date: str, strategy_id: str) -> str:
    """Hash tag -> signal_id: a tag belongs to one stored signal."""
    return f"smx:presig:tags:{trading_date}:{strategy_id}"


def presig_ids(trading_date: str) -> str:
    """Hash signal_id -> '<strategy_id>': which strategy owns an id that day (kept after pickup)."""
    return f"smx:presig:ids:{trading_date}"


def presig_index(trading_date: str) -> str:
    """Set of strategy ids with signals stored for that day, so pickup needs no SCAN."""
    return f"smx:presig:index:{trading_date}"


def presig_lock(trading_date: str) -> str:
    """Set by the morning pickup; intake refuses that day's signals from then on."""
    return f"smx:presig:lock:{trading_date}"


PRESIG_ALGO_KEYS = "smx:presig:algokeys"       # JSON: key_id -> {secret_enc, auth_mode, allowed_strategies, enabled}
PRESIG_INSTRUMENTS_CURRENT = "smx:presig:instruments:current"   # name of the complete instrument hash


def presig_instruments(version: str) -> str:
    """Hash 'EXCH:token' -> compact instrument JSON, one per published master (see publish.py)."""
    return f"smx:presig:instruments:{version}"


def presig_nonce(key_id: str, nonce: str) -> str:
    """A request nonce seen by either intake server, so a replay to the other one is refused."""
    return f"smx:presig:nonce:{key_id}:{nonce}"


def own(key: str) -> str:
    if not key.startswith(PRESIG_PREFIX):
        raise RuntimeError(f"Refusing to write Redis key {key!r}: the next-day store writes only under {PRESIG_PREFIX!r}")
    return key
