"""Redis access for TradePulse, plus an in-process twin for development and tests.

Rules (same discipline as the OMS's ``redis_bridge._own``):

* TradePulse WRITES only keys under ``tradepulse:`` - :func:`own` raises on anything else, so a
  typo can never clobber ``smx:*`` (OMS), ``LTP:*`` (market data) or another system's keys.
* TradePulse READS the OMS event stream with plain ``XREAD`` (no consumer group), so not even
  stream metadata on the OMS key is created. It is used only as a "something changed" signal.
* The ingest queue is a Redis Stream with a consumer group: an event that was read but not
  acknowledged (worker crashed mid-way) is re-claimed and processed again - nothing is lost, and
  the processor is idempotent (unique dedupe key in SQLite).
"""

from __future__ import annotations

import itertools
import json
import threading
import time

PREFIX = "tradepulse:"
INGEST = "tradepulse:ingest"          # stream: webhook / OMS-tail -> processor
INGEST_GROUP = "tp-processor"
LIVE = "tradepulse:live"              # stream: processor -> every API process (SSE fan-out)
HEALTH = "tradepulse:health"          # hash: component -> JSON status
WAKE = "tradepulse:wake"              # key bumped when the OMS stream shows activity


def own(key: str) -> str:
    if not str(key).startswith(PREFIX):
        raise RuntimeError(f"Refusing to write Redis key {key!r}: TradePulse may only write keys under '{PREFIX}'")
    return key


def _enc(data: dict) -> dict:
    return {"d": json.dumps(data, default=str, separators=(",", ":"))}


def _dec(fields: dict) -> dict:
    raw = fields.get("d") if isinstance(fields, dict) else None
    if raw is None:                                   # a foreign stream (the OMS): {"type", "data"}
        return dict(fields or {})
    try:
        return json.loads(raw)
    except ValueError:
        return {}


class RedisBus:
    kind = "redis"

    def __init__(self, client):
        self.r = client

    @classmethod
    def from_settings(cls, s):
        import redis

        # Same credentials as the OMS's .env (REDIS_HOST / REDIS_PORT / REDIS_PASSWORD / REDIS_DB).
        # TP_REDIS_URL is only an optional alternative, used when REDIS_HOST is not set.
        if s.REDIS_URL and not s.REDIS_HOST:
            c = redis.Redis.from_url(s.REDIS_URL, decode_responses=True, socket_connect_timeout=3, socket_timeout=10)
        else:
            c = redis.Redis(host=s.REDIS_HOST or "127.0.0.1", port=s.REDIS_PORT, password=s.REDIS_PASSWORD,
                            db=s.REDIS_DB, decode_responses=True, socket_connect_timeout=3, socket_timeout=10,
                            health_check_interval=30)
        c.ping()
        return cls(c)

    def ping(self) -> bool:
        try:
            return bool(self.r.ping())
        except Exception:
            return False

    # --- streams ----------------------------------------------------------------
    def add(self, stream: str, data: dict, maxlen: int = 50000) -> str:
        return self.r.xadd(own(stream), _enc(data), maxlen=maxlen, approximate=True)

    def ensure_group(self, stream: str, group: str) -> None:
        import redis

        try:
            self.r.xgroup_create(own(stream), group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def read_group(self, stream, group, consumer, count=100, block_ms=0) -> list[tuple[str, dict]]:
        res = self.r.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block_ms or None)
        return [(mid, _dec(f)) for _s, entries in (res or []) for mid, f in entries if f]

    def claim_stale(self, stream, group, consumer, min_idle_ms=30000, count=100) -> list[tuple[str, dict]]:
        """Messages another (dead) consumer read but never acknowledged."""
        try:
            res = self.r.xautoclaim(stream, group, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=count)
        except Exception:
            return []
        entries = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else []
        return [(mid, _dec(f)) for mid, f in entries if f]

    def ack(self, stream, group, message_id) -> None:
        self.r.xack(stream, group, message_id)

    def pending_count(self, stream, group) -> int:
        try:
            return int(self.r.xpending(stream, group).get("pending", 0))
        except Exception:
            return 0

    def read(self, stream: str, last_id: str = "$", count: int = 100, block_ms: int = 0) -> list[tuple[str, dict]]:
        """Plain XREAD - used for the OMS stream (read-only) and the live fan-out stream."""
        res = self.r.xread({stream: last_id}, count=count, block=block_ms or None)
        return [(mid, _dec(f)) for _s, entries in (res or []) for mid, f in entries]

    def last_id(self, stream: str) -> str:
        try:
            info = self.r.xinfo_stream(stream)
            return info.get("last-generated-id") or "0-0"
        except Exception:
            return "0-0"

    def length(self, stream: str) -> int:
        try:
            return int(self.r.xlen(stream))
        except Exception:
            return 0

    # --- key/value ----------------------------------------------------------------
    def hset(self, key, field, value) -> None:
        self.r.hset(own(key), field, value)

    def hgetall(self, key) -> dict:
        return self.r.hgetall(key) or {}

    def set(self, key, value, ttl_s=None) -> None:
        self.r.set(own(key), value, ex=ttl_s)

    def get(self, key):
        return self.r.get(key)

    def incr(self, key, ttl_s=None) -> int:
        v = self.r.incr(own(key))
        if ttl_s:
            self.r.expire(key, ttl_s)
        return int(v)

    def hget_any(self, key, field):
        """Read a field from any hash (e.g. ``LTP:NFO:OPT``) - never written."""
        return self.r.hget(key, field)

    def hgetall_any(self, key) -> dict:
        """Read any hash (``smx:session:<id>``, paper books) - never written."""
        return self.r.hgetall(key) or {}

    def get_any(self, key):
        return self.r.get(key)

    def scan_any(self, pattern: str, limit: int = 2000) -> list[str]:
        out = []
        for k in self.r.scan_iter(match=pattern, count=200):
            out.append(k)
            if len(out) >= limit:
                break
        return out

    def acquire_slot(self, key: str, ms: int) -> tuple[bool, int]:
        """One atomic SET NX PX: True when this caller owns the next ``ms`` window; else (False, ms left)."""
        if self.r.set(own(key), "1", nx=True, px=int(ms)):
            return True, 0
        left = self.r.pttl(key)
        return False, max(1, int(left if left and left > 0 else ms))

    def restart_slot(self, key: str, ms: int) -> None:
        """Re-arm the window from now (after the request finished)."""
        self.r.set(own(key), "1", px=int(ms))


class MemoryBus:
    """Same semantics in-process: consumer groups with pending-until-ack, plain XREAD, KV."""

    kind = "memory"
    _seq = itertools.count(1)

    def __init__(self):
        self._cv = threading.Condition()
        self.streams: dict[str, list] = {}
        self.groups: dict[tuple, dict] = {}
        self.kv: dict = {}
        self.hashes: dict = {}

    def ping(self) -> bool:
        return True

    def add(self, stream, data, maxlen=50000) -> str:
        own(stream)
        with self._cv:
            mid = f"{int(time.time() * 1000)}-{next(self._seq)}"
            lst = self.streams.setdefault(stream, [])
            lst.append((mid, json.loads(json.dumps(data, default=str))))
            self._cv.notify_all()
            return mid

    def add_foreign(self, stream, fields: dict) -> str:
        """Test helper: simulate another system (the OMS) writing its own stream."""
        with self._cv:
            mid = f"{int(time.time() * 1000)}-{next(self._seq)}"
            self.streams.setdefault(stream, []).append((mid, dict(fields)))
            self._cv.notify_all()
            return mid

    def ensure_group(self, stream, group) -> None:
        own(stream)
        with self._cv:
            self.streams.setdefault(stream, [])
            self.groups.setdefault((stream, group), {"next": 0, "pending": {}})

    def read_group(self, stream, group, consumer, count=100, block_ms=0):
        with self._cv:
            g = self.groups[(stream, group)]
            if block_ms and g["next"] >= len(self.streams[stream]):
                self._cv.wait(block_ms / 1000)
            entries = self.streams[stream][g["next"]:g["next"] + count]
            g["next"] += len(entries)
            for mid, d in entries:
                g["pending"][mid] = (consumer, time.monotonic(), d)
            return list(entries)

    def claim_stale(self, stream, group, consumer, min_idle_ms=30000, count=100):
        with self._cv:
            g = self.groups.get((stream, group))
            if not g:
                return []
            out = []
            now = time.monotonic()
            for mid, (c, t, d) in list(g["pending"].items()):
                if (now - t) * 1000 >= min_idle_ms:
                    g["pending"][mid] = (consumer, now, d)
                    out.append((mid, d))
                if len(out) >= count:
                    break
            return out

    def ack(self, stream, group, message_id) -> None:
        with self._cv:
            self.groups[(stream, group)]["pending"].pop(message_id, None)

    def pending_count(self, stream, group) -> int:
        with self._cv:
            g = self.groups.get((stream, group))
            return len(g["pending"]) if g else 0

    def read(self, stream, last_id="$", count=100, block_ms=0):
        with self._cv:
            lst = self.streams.setdefault(stream, [])
            if last_id == "$":
                last_id = lst[-1][0] if lst else "0-0"

            def after():
                return [e for e in lst if _id_gt(e[0], last_id)][:count]
            out = after()
            if not out and block_ms:
                self._cv.wait(block_ms / 1000)
                out = after()
            return out

    def last_id(self, stream) -> str:
        with self._cv:
            lst = self.streams.get(stream) or []
            return lst[-1][0] if lst else "0-0"

    def length(self, stream) -> int:
        with self._cv:
            return len(self.streams.get(stream) or [])

    def hset(self, key, field, value) -> None:
        with self._cv:
            self.hashes.setdefault(own(key), {})[field] = value

    def hgetall(self, key) -> dict:
        with self._cv:
            return dict(self.hashes.get(key, {}))

    def set(self, key, value, ttl_s=None) -> None:
        with self._cv:
            self.kv[own(key)] = value

    def get(self, key):
        with self._cv:
            return self.kv.get(key)

    def incr(self, key, ttl_s=None) -> int:
        with self._cv:
            v = int(self.kv.get(own(key)) or 0) + 1
            self.kv[key] = str(v)
            return v

    def hget_any(self, key, field):
        with self._cv:
            return self.hashes.get(key, {}).get(field)

    def hgetall_any(self, key) -> dict:
        with self._cv:
            return dict(self.hashes.get(key, {}))

    def get_any(self, key):
        with self._cv:
            return self.kv.get(key)

    def scan_any(self, pattern: str, limit: int = 2000) -> list[str]:
        import fnmatch
        with self._cv:
            keys = set(self.streams) | set(self.kv) | set(self.hashes)
            return sorted(k for k in keys if fnmatch.fnmatchcase(k, pattern))[:limit]

    def acquire_slot(self, key: str, ms: int) -> tuple[bool, int]:
        with self._cv:
            own(key)
            now = time.monotonic()
            self._slots = getattr(self, "_slots", {})
            until = self._slots.get(key, 0.0)
            if until > now:
                return False, max(1, int((until - now) * 1000))
            self._slots[key] = now + ms / 1000
            return True, 0

    def restart_slot(self, key: str, ms: int) -> None:
        with self._cv:
            own(key)
            self._slots = getattr(self, "_slots", {})
            self._slots[key] = time.monotonic() + ms / 1000

    # test helpers: simulate keys owned by other systems (the OMS)
    def set_foreign(self, key, value) -> None:
        with self._cv:
            self.kv[key] = value

    def hset_foreign(self, key, mapping: dict) -> None:
        with self._cv:
            self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})


def _id_gt(a: str, b: str) -> bool:
    try:
        a1, a2 = (int(x) for x in a.split("-"))
        b1, b2 = (int(x) for x in b.split("-"))
        return (a1, a2) > (b1, b2)
    except ValueError:
        return a > b


def make_bus(s):
    """Redis when reachable; the in-process bus only when explicitly asked for (dev/tests)."""
    if s.USE_MEMORY_BUS:
        return MemoryBus()
    return RedisBus.from_settings(s)
