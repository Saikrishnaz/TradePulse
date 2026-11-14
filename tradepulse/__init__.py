"""TradePulse - monitoring, notification and assistant layer for the SkyMatrix OMS.

TradePulse never places, modifies or cancels orders and never calls a broker API.
It reads the OMS (SQLite read-only + the ``smx:oms:events`` stream as a wake-up
signal), accepts broker webhooks, and writes only its own database and Redis keys
under ``tradepulse:``.
"""

__version__ = "1.0.0"
