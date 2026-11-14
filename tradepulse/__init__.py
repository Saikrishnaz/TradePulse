"""TradePulse - monitoring and notification layer for the SkyMatrix OMS.

TradePulse never places, modifies or cancels orders.
It follows the OMS only through Redis (read-only), reads positions/holdings from
brokers only at 09:00/09:05 or on a user's request (1 request / 3 s), and writes
only its own database and Redis keys under ``tradepulse:``.
"""

__version__ = "1.0.0"
