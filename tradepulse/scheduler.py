"""Scheduled group reports - the "how often to report" half of monitoring frequency.

Per group (Groups -> Schedule):
* ``digest_interval_min``  - trading summary every N minutes during market hours (0 = off)
* ``digest_times``         - extra summaries at fixed IST times, e.g. ["09:30", "12:00"]
* ``position_interval_min``- open-positions report every N minutes during market hours (0 = off)
* ``eod_summary``          - end-of-day summary at ``eod_summary_time`` on trading days

Reports read the OMS database only. ``schedule_state`` records the last run of each schedule so a
restart neither skips nor repeats one.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta

from . import bus as busmod
from . import reports
from .config import IST, ist_iso, now_ist
from .notify.outbox import queue_message

log = logging.getLogger("tradepulse.scheduler")


def _hm(s: str, base: datetime) -> datetime | None:
    try:
        hh, mm = (int(x) for x in str(s).split(":"))
        return base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (ValueError, TypeError):
        return None


def in_market(settings: dict, now: datetime) -> bool:
    if now.weekday() not in settings.get("trading_days", [0, 1, 2, 3, 4]):
        return False
    o, c = _hm(settings.get("market_open", "09:15"), now), _hm(settings.get("market_close", "15:30"), now)
    return bool(o and c and o <= now <= c)


class Scheduler:
    def __init__(self, store, bus, svc):
        self.store, self.bus, self.svc = store, bus, svc

    def _last(self, name: str) -> datetime | None:
        r = self.store.one("SELECT last_at FROM schedule_state WHERE name = ?", (name,))
        if not r:
            return None
        try:
            return datetime.fromisoformat(r["last_at"])
        except ValueError:
            return None

    def _mark(self, name: str, at: datetime) -> None:
        self.store.execute("""INSERT INTO schedule_state (name, last_at) VALUES (?, ?)
                              ON CONFLICT(name) DO UPDATE SET last_at = excluded.last_at""",
                           (name, at.astimezone(IST).isoformat(timespec="seconds")))

    def due_jobs(self, now: datetime | None = None) -> list[tuple[dict, str, str]]:
        """(group, kind, schedule-name) for every report due now."""
        now = now or now_ist()
        settings = self.store.get_settings()
        market = in_market(settings, now)
        trading_day = now.weekday() in settings.get("trading_days", [0, 1, 2, 3, 4])
        jobs = []
        for g in self.store.groups():
            if not g["enabled"] or not g["channels"] or not g["account_ids"]:
                continue
            gid = g["id"]
            iv = int(g.get("digest_interval_min") or 0)
            if iv > 0 and market:
                last = self._last(f"g{gid}:digest")
                if last is None or (now - last).total_seconds() >= iv * 60:
                    jobs.append((g, "digest", f"g{gid}:digest"))
            piv = int(g.get("position_interval_min") or 0)
            if piv > 0 and market:
                last = self._last(f"g{gid}:positions")
                if last is None or (now - last).total_seconds() >= piv * 60:
                    jobs.append((g, "positions", f"g{gid}:positions"))
            if trading_day:
                for t in g.get("digest_times") or []:
                    at = _hm(t, now)
                    name = f"g{gid}:at:{t}"
                    last = self._last(name)
                    if at and now >= at and (last is None or last < at) and (now - at).total_seconds() < 3600:
                        jobs.append((g, "digest", name))
                if g.get("eod_summary"):
                    at = _hm(settings.get("eod_summary_time", "15:40"), now)
                    last = self._last(f"g{gid}:eod")
                    if at and now >= at and (last is None or last < at) and (now - at).total_seconds() < 3 * 3600:
                        jobs.append((g, "eod", f"g{gid}:eod"))
        return jobs

    def run_once(self, now: datetime | None = None) -> int:
        now = now or now_ist()
        n = 0
        for g, kind, name in self.due_jobs(now):
            try:
                text = reports.digest_text(self.svc, g, kind)
                for ch in g["channels"]:
                    if ch["enabled"] and ch["status"] == "ACTIVE":
                        queue_message(self.store, ch["id"], text, kind=kind, group_id=g["id"])
                        n += 1
                self._mark(name, now)
            except Exception:
                log.exception("scheduled %s for group %s failed", kind, g["name"])
        return n

    def send_now(self, group: dict, kind: str = "digest") -> int:
        text = reports.digest_text(self.svc, group, kind)
        n = 0
        for ch in group["channels"]:
            if ch["enabled"] and ch["status"] == "ACTIVE":
                queue_message(self.store, ch["id"], text, kind=kind, group_id=group["id"])
                n += 1
        return n

    def housekeeping(self, now: datetime | None = None) -> dict:
        """Retention: events and delivered/skipped notifications older than ``event_retention_days``; the
        webhook log beyond its newest 20,000 rows; expired sessions and link codes. Undelivered
        (QUEUED/FAILED/DEAD) notifications are never pruned - they stay until an admin retries them."""
        now = now or now_ist()
        days = int(self.store.setting("event_retention_days") or 30)
        cutoff = (now - timedelta(days=days)).astimezone(IST).isoformat(timespec="seconds")
        nowiso = now.astimezone(IST).isoformat(timespec="seconds")
        with self.store.tx() as c:
            n = c.execute("DELETE FROM notifications WHERE status IN ('SENT','SKIPPED') AND created_at < ?", (cutoff,)).rowcount
            e = c.execute("""DELETE FROM events WHERE received_at < ? AND event_id NOT IN
                             (SELECT event_id FROM notifications WHERE event_id IS NOT NULL)""", (cutoff,)).rowcount
            c.execute("DELETE FROM webhook_log WHERE id <= (SELECT MAX(id) FROM webhook_log) - 20000")
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (nowiso,))
            c.execute("DELETE FROM link_codes WHERE expires_at < ?", (nowiso,))
            c.execute("DELETE FROM assistant_log WHERE at < ?", (cutoff,))
            # broker snapshots: keep the ones that are a current baseline, drop old payloads
            c.execute("""DELETE FROM snapshots WHERE at < ? AND id NOT IN
                         (SELECT snapshot_id FROM book_baselines WHERE snapshot_id IS NOT NULL)""", (cutoff,))
            c.execute("DELETE FROM reconcile_diffs WHERE at < ?", (cutoff,))
            c.execute("DELETE FROM fills WHERE created_at < ?", (cutoff,))
            c.execute("DELETE FROM order_events WHERE at < ?", (cutoff,))
            c.execute("DELETE FROM orders WHERE updated_at < ? AND status IN ('COMPLETE','REJECTED','CANCELLED','FAILED','EXPIRED')",
                      (cutoff,))
        return {"events": e, "notifications": n}

    def run(self, stop: threading.Event) -> None:
        log.info("scheduler started")
        last_house = 0.0
        while not stop.is_set():
            try:
                if time.monotonic() - last_house > 3600:
                    self.housekeeping()
                    last_house = time.monotonic()
                self.run_once()
                self.bus.hset(busmod.HEALTH, "scheduler", json.dumps({"at": ist_iso()}))
            except Exception:
                log.exception("scheduler loop error")
            stop.wait(20)
