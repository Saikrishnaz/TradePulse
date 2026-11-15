"""Next-day signal intake: POST /api/signals/next-day stores a signal for the next session in Redis.

TradePulse never places orders; the SkyMatrix OMS picks the stored signals up the next morning.

    contract.py, store.py, keys.py   byte-identical copies of backend/oms/presignal/ in SkyMatrix_Dashboard
                                     (tests/test_presignal.py fails if they drift, when that repo is present)
    intake.py                        TradePulse's own handler: auth from the OMS-published keys, validation
                                     against the OMS-published templates and instrument master

The one place TradePulse writes outside ``tradepulse:``: keys under ``smx:presig:``, through
``keys.own`` (which refuses anything else). ``bus.own`` is unchanged.
"""
