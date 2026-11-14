"""Environment configuration. Secrets come only from the environment (or are stored encrypted).

Runtime knobs an operator changes day to day (monitoring frequency, market hours, retry policy)
are NOT here - they live in the ``settings`` table and are edited from the dashboard.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is in requirements
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if load_dotenv:
    load_dotenv(PROJECT_ROOT / ".env")

IST = timezone(timedelta(hours=5, minutes=30))


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def now_ist() -> datetime:
    return datetime.now(IST)


def ist_iso(dt: datetime | None = None) -> str:
    return (dt or now_ist()).astimezone(IST).isoformat(timespec="seconds")


def ist_iso_ms(dt: datetime | None = None) -> str:
    return (dt or now_ist()).astimezone(IST).isoformat(timespec="milliseconds")


def parse_ts(s: str) -> datetime | None:
    """ISO / broker timestamp -> aware datetime (IST if no offset). Second-resolution stamps are the start of the second."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).strip().replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=IST)


def today_ist() -> str:
    return now_ist().strftime("%Y-%m-%d")


class Settings:
    """Read once at start-up; tests build their own instance."""

    def __init__(self, **overrides):
        g = lambda k, d="": overrides.get(k, os.getenv(k, d))  # noqa: E731
        # --- OMS: Redis only. TradePulse never connects to the OMS database or API. ----------
        self.OMS_EVENTS_STREAM = g("TP_OMS_EVENTS_STREAM", "smx:oms:events")
        self.OMS_COMMAND_STREAMS = g("TP_OMS_COMMAND_STREAMS", "smx:oms:cmd:*")
        # Kept only so `python -m tradepulse.admin import-oms` can be pointed at a file once.
        self.OMS_DB_PATH = Path(g("TP_OMS_DB_PATH", "")) if g("TP_OMS_DB_PATH", "") else None

        # --- read-only broker snapshots (positions / holdings) ----------
        # Overrides for testing only; the real hosts are defined in tradepulse/brokers.py and nowhere else.
        self.KITE_API = g("TP_KITE_API", "")
        self.ANGEL_API = g("TP_ANGEL_API", "")
        self.FIVEPAISA_API = g("TP_FIVEPAISA_API", "")

        # --- bootstrap admin (created once when TradePulse has no users) ----------
        self.ADMIN_USERNAME = g("TP_ADMIN_USERNAME", "admin")
        self.ADMIN_PASSWORD = g("TP_ADMIN_PASSWORD", "")

        # --- TradePulse own storage ----------------------------------
        self.DB_PATH = Path(g("TP_DB_PATH", str(PROJECT_ROOT / "data" / "tradepulse.db")))
        self.FERNET_KEY = g("TP_FERNET_KEY", "")          # encrypts bot tokens / webhook secrets at rest
        self.SECRET_KEY = g("TP_SECRET_KEY", "")

        # --- Redis (shared instance; TradePulse writes only tradepulse:*) ----
        self.REDIS_URL = g("TP_REDIS_URL", "")
        self.REDIS_HOST = g("REDIS_HOST", "")
        self.REDIS_PORT = int(overrides.get("REDIS_PORT", _int("REDIS_PORT", 6379)))
        self.REDIS_PASSWORD = g("REDIS_PASSWORD", "") or None
        self.REDIS_DB = int(overrides.get("REDIS_DB", _int("REDIS_DB", 0)))
        self.USE_MEMORY_BUS = bool(overrides.get("USE_MEMORY_BUS", _bool("TP_MEMORY_BUS", False)))

        # --- web --------------------------------------------------------
        self.HOST = g("TP_HOST", "127.0.0.1")
        self.PORT = int(overrides.get("PORT", _int("TP_PORT", 8895)))
        self.COOKIE_NAME = "tp_session"
        self.COOKIE_SECURE = bool(overrides.get("COOKIE_SECURE", _bool("TP_COOKIE_SECURE", False)))
        self.SESSION_HOURS = int(overrides.get("SESSION_HOURS", _int("TP_SESSION_HOURS", 12)))
        self.PUBLIC_BASE_URL = g("TP_PUBLIC_BASE_URL", "").rstrip("/")
        # Run ingest/processor/notifier/bot inside the web process (single-box). Set 0 and run
        # `python -m tradepulse.worker` to deploy them independently.
        self.RUN_WORKERS = bool(overrides.get("RUN_WORKERS", _bool("TP_RUN_WORKERS", True)))

        # --- Telegram ---------------------------------------------------
        self.TELEGRAM_BOT_TOKEN = g("TP_TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_API = g("TP_TELEGRAM_API", "https://api.telegram.org")
        self.TELEGRAM_MODE = g("TP_TELEGRAM_MODE", "polling")      # polling | webhook | off

        # --- AI assistant -----------------------------------------------
        self.AI_MODEL = g("TP_AI_MODEL", "claude-opus-5-5")
        self.AI_EFFORT = g("TP_AI_EFFORT", "low")
        self.AI_ENABLED = bool(overrides.get("AI_ENABLED", _bool("TP_AI_ENABLED", True)))
        self.AI_FALLBACKS = bool(overrides.get("AI_FALLBACKS", _bool("TP_AI_FALLBACKS", True)))

        # --- webhooks ---------------------------------------------------
        self.WEBHOOK_RATE_PER_MIN = int(overrides.get("WEBHOOK_RATE_PER_MIN", _int("TP_WEBHOOK_RATE_PER_MIN", 600)))

        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
