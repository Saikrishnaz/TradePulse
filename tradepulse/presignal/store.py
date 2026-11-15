"""Redis store for next-day signal snapshots.

One snapshot per signal_id per session, under ``smx:presig:`` (keys in ``redis_keys``):

    day:<date>:<strategy>       hash  signal_id -> current snapshot (what the morning executes)
    versions:<date>:<strategy>  hash  "<signal_id>:<v>" -> every version, kept 30 days
    tags:<date>:<strategy>      hash  tag -> signal_id (a tag belongs to one stored signal)
    ids:<date>                  hash  signal_id -> strategy (kept after pickup: an id is used once)
    index:<date>                set   strategies with something stored
    lock:<date>                 str   the morning pickup has started: nothing more is taken

A write is one optimistic transaction (WATCH ... MULTI/EXEC): every key changes together or none
does, and a reader sees either the previous snapshot or the complete new one. Each snapshot is a
single hash field holding the whole JSON plus the sha256 of its payload, which the reader checks.

Shared with TradePulse, which carries a byte-identical copy (tradepulse/presignal/store.py): no
database and nothing from the OMS, only a redis-py client.
"""

import hashlib
import json

from . import keys as rk

DAY_TTL_S = 10 * 86400          # current snapshots, tags, ids, index, lock
VERSIONS_TTL_S = 30 * 86400     # version history
WATCH_RETRIES = 8


class StoreRefused(Exception):
    """The store said no (the signal is wrong for the store's state), not a Redis failure."""

    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


class StoreUnavailable(Exception):
    """Redis could not be reached or stayed contended: nothing was stored."""


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def intact(snapshot: dict) -> bool:
    """The snapshot's payload still matches the hash written with it."""
    return isinstance(snapshot, dict) and isinstance(snapshot.get("payload"), dict) \
        and payload_sha256(snapshot["payload"]) == snapshot.get("payload_sha256")


class PresignalStore:
    def __init__(self, client):
        self.r = client

    # --- writing --------------------------------------------------------
    def put(self, snap: dict) -> tuple[str, int]:
        """Store ``snap`` as the signal's current snapshot. Returns ("STORED" | "DUPLICATE", version).

        Refuses (StoreRefused) when the day is locked, the id belongs to another strategy or was
        already picked up, the version does not go up, or a tag is held by another stored signal."""
        import redis

        d, s, sid, v = snap["trading_date"], snap["strategy_id"], snap["signal_id"], int(snap["version"])
        day, versions, tags = rk.presig_day(d, s), rk.presig_versions(d, s), rk.presig_tags(d, s)
        id_map, index, lock = rk.presig_ids(d), rk.presig_index(d), rk.presig_lock(d)
        for key in (day, versions, tags, id_map, index, lock):
            rk.own(key)
        blob = _dump(snap)
        try:
            with self.r.pipeline() as pipe:
                for _ in range(WATCH_RETRIES):
                    try:
                        pipe.watch(day, tags, id_map, lock)
                        if pipe.exists(lock):
                            raise StoreRefused("SIGNAL_LOCKED", f"the {d} signals were already picked up by the OMS")
                        owner = pipe.hget(id_map, sid)
                        if owner and owner != s:
                            raise StoreRefused("SIGNAL_ID_TAKEN", f"signal_id {sid} is already used by another strategy")
                        raw = pipe.hget(day, sid)
                        current = json.loads(raw) if raw else None
                        if current is None and owner:
                            raise StoreRefused("SIGNAL_LOCKED", f"signal {sid} was already picked up")
                        if current is not None:
                            if current.get("payload_sha256") == snap["payload_sha256"]:
                                pipe.unwatch()
                                return "DUPLICATE", int(current["version"])
                            if v <= int(current["version"]):
                                raise StoreRefused("STALE_VERSION", f"version {current['version']} is stored; "
                                                                    f"send version {int(current['version']) + 1} or higher")
                        held = {t: pipe.hget(tags, t) for t in snap["tags"]}
                        clash = sorted(t for t, owner_sid in held.items() if owner_sid and owner_sid != sid)
                        if clash:
                            raise StoreRefused("TAG_ALREADY_STORED", f"tag {', '.join(clash)} already belongs to "
                                                                     f"stored signal {held[clash[0]]}")
                        dropped = [t for t in (current or {}).get("tags", []) if t not in snap["tags"]]
                        pipe.multi()
                        pipe.hset(day, sid, blob)
                        pipe.hset(versions, f"{sid}:{v}", blob)
                        for t in snap["tags"]:
                            pipe.hset(tags, t, sid)
                        for t in dropped:
                            pipe.hdel(tags, t)
                        pipe.hset(id_map, sid, s)
                        pipe.sadd(index, s)
                        for key in (day, tags, id_map, index):
                            pipe.expire(key, DAY_TTL_S)
                        pipe.expire(versions, VERSIONS_TTL_S)
                        pipe.execute()
                        return "STORED", v
                    except redis.WatchError:
                        continue
        except StoreRefused:
            raise
        except redis.RedisError as e:
            raise StoreUnavailable(str(e)) from e
        raise StoreUnavailable("the store stayed busy; resend the same signal")

    def lock(self, trading_date: str, by: str) -> bool:
        """Close intake for the day. True if this call set the lock."""
        return bool(self.r.set(rk.own(rk.presig_lock(trading_date)), by, nx=True, ex=DAY_TTL_S))

    def locked(self, trading_date: str) -> bool:
        return bool(self.r.exists(rk.presig_lock(trading_date)))

    def remove(self, trading_date: str, strategy_id: str, signal_id: str) -> bool:
        """Drop a picked-up signal's current snapshot and free its tags. Its id stays taken and its
        versions stay for audit. Safe to repeat."""
        import redis

        day, tags, index = rk.presig_day(trading_date, strategy_id), rk.presig_tags(trading_date, strategy_id), \
            rk.presig_index(trading_date)
        for key in (day, tags, index):
            rk.own(key)
        with self.r.pipeline() as pipe:
            for _ in range(WATCH_RETRIES):
                try:
                    pipe.watch(day, tags)
                    raw = pipe.hget(day, signal_id)
                    if not raw:
                        pipe.unwatch()
                        return False
                    snap_tags = (json.loads(raw).get("tags") or [])
                    mine = [t for t in snap_tags if pipe.hget(tags, t) == signal_id]
                    remaining = pipe.hlen(day) - 1
                    pipe.multi()
                    pipe.hdel(day, signal_id)
                    for t in mine:
                        pipe.hdel(tags, t)
                    if remaining <= 0:
                        pipe.srem(index, strategy_id)
                    pipe.execute()
                    return True
                except redis.WatchError:
                    continue
        raise StoreUnavailable("the store stayed busy while removing a picked-up signal")

    # --- reading --------------------------------------------------------
    def get(self, trading_date: str, strategy_id: str, signal_id: str) -> dict | None:
        raw = self.r.hget(rk.presig_day(trading_date, strategy_id), signal_id)
        return json.loads(raw) if raw else None

    def versions(self, trading_date: str, strategy_id: str, signal_id: str) -> list[dict]:
        rows = self.r.hgetall(rk.presig_versions(trading_date, strategy_id)) or {}
        out = [json.loads(v) for k, v in rows.items() if k.rsplit(":", 1)[0] == signal_id]
        return sorted(out, key=lambda s: int(s["version"]))

    def pending(self, trading_date: str) -> tuple[list[dict], list[dict]]:
        """(intact snapshots, damaged ones as {"strategy_id", "signal_id", "reason"}) for the day."""
        good, bad = [], []
        for strategy_id in sorted(self.r.smembers(rk.presig_index(trading_date)) or []):
            for sid, raw in sorted((self.r.hgetall(rk.presig_day(trading_date, strategy_id)) or {}).items()):
                try:
                    snap = json.loads(raw)
                except ValueError:
                    bad.append({"strategy_id": strategy_id, "signal_id": sid, "reason": "not valid JSON"})
                    continue
                if not intact(snap):
                    bad.append({"strategy_id": strategy_id, "signal_id": sid, "reason": "payload hash mismatch"})
                    continue
                good.append(snap)
        return good, bad

    # --- templates (publish.py writes, TradePulse reads) -----------------
    def strategy_template(self, strategy_id: str) -> dict | None:
        raw = self.r.get(rk.presig_strategy(strategy_id))
        return json.loads(raw) if raw else None
