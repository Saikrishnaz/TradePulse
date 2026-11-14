"""TradePulse web application: dashboard API, live event stream (SSE), broker webhooks, Telegram webhook.

    python -m tradepulse.app      -> http://127.0.0.1:8895
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__
from . import bus as busmod
from .auth import Auth, public_user
from .bot import create_link_code
from .config import IST, PROJECT_ROOT, ist_iso, today_ist
from .events import DEFAULT_ENABLED, EVENT_LABELS, EVENT_TYPES
from .notify.format import h
from .notify.outbox import queue_message
from .localdata import mask_account
from .processor import enqueue
from .rules import CHANNEL_KEYS, RuleSnapshot, event_enabled, explain
from .runtime import Runtime
from .service import Scope
from .sources.webhooks import WebhookRejected
from .store import DEFAULT_SETTINGS

log = logging.getLogger("tradepulse.app")
WEB_DIR = PROJECT_ROOT / "web"
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# --- live fan-out -------------------------------------------------------------------------------
class LiveHub:
    """One reader of ``tradepulse:live`` per process, fanned out to SSE clients by scope."""

    def __init__(self, bus):
        self.bus = bus
        self.subs: list[tuple[asyncio.Queue, asyncio.AbstractEventLoop, set]] = []
        self._lock = threading.Lock()

    def subscribe(self, scope_ids: set) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self.subs.append((q, asyncio.get_running_loop(), scope_ids))
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            self.subs = [s for s in self.subs if s[0] is not q]

    def _push(self, q: asyncio.Queue, data: dict) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(data)

    def run(self, stop: threading.Event) -> None:
        last = self.bus.last_id(busmod.LIVE)
        while not stop.is_set():
            try:
                for mid, data in self.bus.read(busmod.LIVE, last, count=200, block_ms=2000):
                    last = mid
                    with self._lock:
                        subs = list(self.subs)
                    for q, loop, ids in subs:
                        if data.get("account_id") in ids:
                            loop.call_soon_threadsafe(self._push, q, data)
            except Exception as ex:
                log.debug("live hub read: %s", ex)
                stop.wait(2)


# --- request models -----------------------------------------------------------------------------
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SelectIn(BaseModel):
    account_ids: list[str] = Field(max_length=2000)
    enabled: bool = True


class MuteIn(BaseModel):
    muted: bool


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    enabled: bool = True
    color: str = Field(default="#3b82f6", pattern=r"^#[0-9a-fA-F]{6}$")
    delivery_mode: str = Field(default="instant", pattern=r"^(instant|batched)$")
    batch_minutes: int = Field(default=5, ge=1, le=240)
    digest_interval_min: int = Field(default=0, ge=0, le=1440)
    digest_times: list[str] = Field(default_factory=list, max_length=24)
    position_interval_min: int = Field(default=0, ge=0, le=1440)
    eod_summary: bool = True
    allow_queries: bool = True
    account_ids: list[str] = Field(default_factory=list, max_length=2000)
    channel_ids: list[int] = Field(default_factory=list, max_length=200)
    rules: dict[str, bool | None] = Field(default_factory=dict)
    # [] = follow the global setting. None (field not sent) = keep what the group has: a client that does not
    # know this field (an old dashboard tab) must never reset it. (Live: a stale tab wiped LIVE-only.)
    modes: list[str] | None = Field(default=None, max_length=3)

    @field_validator("modes")
    @classmethod
    def _modes(cls, v):
        if v is None:
            return None
        v = sorted({str(m).upper() for m in v})
        bad = [m for m in v if m not in ("LIVE", "PAPER", "DRY_RUN")]
        if bad:
            raise ValueError(f"unknown trading mode(s): {', '.join(bad)}")
        return v

    @field_validator("digest_times")
    @classmethod
    def _times(cls, v):
        for t in v:
            if not HHMM.match(t):
                raise ValueError(f"time {t!r} must be HH:MM (24h, IST)")
        return sorted(set(v))


class RulesIn(BaseModel):
    scope: str = Field(pattern=r"^(global|client|account|group)$")
    scope_id: str = Field(default="", max_length=64)
    rules: dict[str, bool | None]


class TestIn(BaseModel):
    channel_id: int
    text: str = Field(default="", max_length=1000)


class TokenIn(BaseModel):
    token: str = Field(default="", max_length=200)


class ChatIn(BaseModel):
    chat_id: str = Field(min_length=1, max_length=40, pattern=r"^-?\d+$|^\+?\d{8,15}$")
    title: str = Field(default="", max_length=120)
    kind: str = Field(default="telegram", pattern=r"^(telegram|whatsapp)$")


class ChatUpdate(BaseModel):
    enabled: bool | None = None
    status: str | None = Field(default=None, pattern=r"^(PENDING|ACTIVE|DISABLED)$")
    title: str | None = Field(default=None, max_length=120)
    group_ids: list[int] | None = None


class SecretIn(BaseModel):
    secret: str = Field(default="", max_length=500)
    account_id: str = Field(default="", max_length=64)


class WhatsAppIn(BaseModel):
    token: str = Field(default="", max_length=500)
    phone_number_id: str = Field(default="", max_length=64)
    template: str = Field(default="", max_length=100)


class RefreshIn(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(pattern=r"^(positions|holdings)$")


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(pattern=r"^(ADMIN|MANAGER|CLIENT)$")
    status: str = Field(default="ACTIVE", pattern=r"^(ACTIVE|DISABLED)$")
    manager_id: int | None = None
    password: str = Field(default="", max_length=200)
    account_ids: list[str] | None = Field(default=None, max_length=500)


SETTINGS_RULES = {
    "broker_slot_seconds": (int, 3, 60),          # never below 3: the broker-load limit is a floor, not a target
    "oms_poll_interval_ms": (int, 250, 60000), "retry_max_attempts": (int, 1, 50), "retry_base_seconds": (int, 1, 600),
    "event_retention_days": (int, 1, 3650), "default_digest_interval_min": (int, 0, 1440),
}


def create_app(settings=None, runtime: Runtime | None = None, start_workers: bool | None = None) -> FastAPI:
    from .config import settings as default_settings

    settings = settings or default_settings
    rt = runtime or Runtime(settings)
    auth = Auth(rt.store, rt.oms, settings)
    limiter = auth.limiter
    hub = LiveHub(rt.bus)
    run_workers = settings.RUN_WORKERS if start_workers is None else start_workers

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        threading.Thread(target=hub.run, args=(rt.stop,), daemon=True, name="tp-livehub").start()
        if run_workers:
            rt.start_workers()
        yield
        rt.shutdown()

    app = FastAPI(title="TradePulse", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.rt = rt

    # --- middleware ---------------------------------------------------------------------------
    @app.middleware("http")
    async def security(request: Request, call_next):
        path = request.url.path
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and path.startswith("/api/") \
                and not path.startswith(("/api/webhooks/brokers/", "/api/telegram/webhook/")) \
                and request.headers.get("x-tradepulse") != "1":
            return JSONResponse({"detail": "Missing X-TradePulse header"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        return resp

    # --- dependencies ---------------------------------------------------------------------------
    def user(request: Request) -> dict:
        return auth.current(request)

    def admin(u: dict = Depends(user)) -> dict:
        if u["role"] != "ADMIN":
            raise HTTPException(403, "Only an administrator can change TradePulse configuration.")
        return u

    def scope(u: dict = Depends(user)) -> Scope:
        return rt.svc.scope_for_user(u)

    def changed(actor: dict, action: str, target="", detail="") -> None:
        rt.store.audit(actor["username"], action, target, detail)
        rt.processor.invalidate()

    # --- pages -----------------------------------------------------------------------------------
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "at": ist_iso()}

    # --- auth ----------------------------------------------------------------------------------------
    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request, response: Response):
        return auth.login(body.username, body.password, request, response)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        auth.logout(request, response)
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(u: dict = Depends(user)):
        out = public_user(u)
        tg = rt.store.one("SELECT telegram_user_id, username, linked_at FROM tg_users WHERE oms_user_id = ? "
                          "ORDER BY linked_at DESC LIMIT 1", (u["id"],))
        out["telegram"] = tg
        return out

    # --- overview / accounts ---------------------------------------------------------------------------
    @app.get("/api/overview")
    def overview(day: str | None = None, sc: Scope = Depends(scope)):
        data = rt.svc.overview(sc, _day(day))
        data["health"] = _health_brief()
        data["stats"] = rt.svc.event_stats(sc, _day(day))
        return data

    @app.get("/api/clients")
    def clients(sc: Scope = Depends(scope)):
        accts = rt.svc.accounts(sc)
        by: dict = {}
        for a in accts:
            c = by.setdefault(a["user_id"], {"client_id": a["user_id"], "name": a["client_name"], "role": a["role"],
                                             "status": a["user_status"], "manager_name": a["manager_name"], "accounts": []})
            c["accounts"].append(_acct(a))
        return sorted(by.values(), key=lambda c: (c["name"] or "").lower())

    @app.get("/api/clients/{client_id}/accounts")
    def client_accounts(client_id: int, sc: Scope = Depends(scope)):
        return [_acct(a) for a in rt.svc.accounts(sc) if a["user_id"] == client_id]

    @app.get("/api/monitoring/accounts")
    def monitoring_accounts(sc: Scope = Depends(scope)):
        return [_acct(a) for a in rt.svc.accounts(sc)]

    @app.post("/api/monitoring/accounts/select")
    def monitoring_select(body: SelectIn, u: dict = Depends(admin)):
        known = set(rt.oms.visible_account_ids(u))
        ids = [a for a in body.account_ids if a in known]
        rt.store.set_monitoring(ids, body.enabled, u["username"])
        changed(u, "MONITORING_" + ("ENABLED" if body.enabled else "DISABLED"), ",".join(ids)[:500])
        return {"updated": len(ids)}

    @app.post("/api/monitoring/accounts/{account_id}/enable")
    def monitoring_enable(account_id: str, u: dict = Depends(admin)):
        return monitoring_select(SelectIn(account_ids=[account_id], enabled=True), u)

    @app.post("/api/monitoring/accounts/{account_id}/disable")
    def monitoring_disable(account_id: str, u: dict = Depends(admin)):
        return monitoring_select(SelectIn(account_ids=[account_id], enabled=False), u)

    @app.post("/api/monitoring/accounts/{account_id}/mute")
    def monitoring_mute(account_id: str, body: MuteIn, u: dict = Depends(admin)):
        _need(rt.svc.scope_for_user(u), account_id)
        rt.store.set_muted(account_id, body.muted, u["username"])
        changed(u, "ACCOUNT_MUTED" if body.muted else "ACCOUNT_UNMUTED", account_id)
        return {"ok": True}

    @app.get("/api/accounts/{account_id}")
    def account_detail(account_id: str, sc: Scope = Depends(scope)):
        d = rt.svc.account_detail(sc, account_id)
        if not d:
            raise HTTPException(404, "Account not found")
        d["account"] = _acct(d["account"])
        snap = RuleSnapshot.load(rt.store, {account_id: d["account"]["user_id"]})
        d["notification_rules"] = {et: {"enabled": event_enabled(snap, et, account_id, None)[0],
                                        "decided_by": event_enabled(snap, et, account_id, None)[1],
                                        "account_override": snap.rules.get(("account", account_id), {}).get(et)}
                                   for et in EVENT_TYPES}
        return _clean(d)

    # --- orders / positions / trades / performance ---------------------------------------------------
    @app.get("/api/orders")
    def orders(account_id: str | None = None, status: str | None = None, day: str | None = None,
               search: str | None = None, limit: int = 300, sc: Scope = Depends(scope)):
        ids = _ids(sc, account_id)
        rows = rt.oms.orders(ids, day=_day(day) if day != "all" else None, status=status, search=search,
                             limit=min(limit, 2000))
        for r in rows:
            r["masked_account"] = mask_account(r["account_id"])
        return rows

    @app.get("/api/orders/{order_ref}")
    def order(order_ref: str, sc: Scope = Depends(scope)):
        o = rt.oms.order(sc.account_ids, order_ref)
        if not o:
            raise HTTPException(404, "Order not found")
        o["masked_account"] = mask_account(o["account_id"])
        return o

    @app.get("/api/positions")
    def positions(account_id: str | None = None, state: str = "open", sc: Scope = Depends(scope)):
        if state not in ("open", "closed", "all"):
            raise HTTPException(400, "state must be open, closed or all")
        rows = rt.oms.positions(_ids(sc, account_id), state, day=today_ist() if state != "open" else None, ltp=rt.svc.ltp)
        return [_clean(r) for r in rows]

    @app.get("/api/broker-positions")
    def broker_positions(account_id: str | None = None, sc: Scope = Depends(scope)):
        return rt.oms.broker_positions(_ids(sc, account_id))

    @app.get("/api/trades")
    def trades(account_id: str | None = None, day: str | None = None, sc: Scope = Depends(scope)):
        rows = rt.oms.fills(_ids(sc, account_id), day=_day(day) if day != "all" else None, limit=2000)
        for r in rows:
            r["masked_account"] = mask_account(r["account_id"])
        return rows

    @app.get("/api/performance")
    def performance(period: str = "today", sc: Scope = Depends(scope)):
        return rt.svc.performance(sc, period)

    @app.get("/api/alerts")
    def alerts(account_id: str | None = None, sc: Scope = Depends(scope)):
        return rt.oms.alerts(_ids(sc, account_id), limit=100)

    # --- events ----------------------------------------------------------------------------------
    @app.get("/api/events")
    def events(request: Request, sc: Scope = Depends(scope)):
        return rt.svc.events(sc, **_event_filters(request))

    @app.get("/api/events/stats")
    def event_stats(day: str | None = None, sc: Scope = Depends(scope)):
        return rt.svc.event_stats(sc, _day(day))

    @app.get("/api/events/export.csv")
    def export_events(request: Request, sc: Scope = Depends(scope)):
        f = _event_filters(request)
        f["limit"] = 5000
        rows = rt.svc.events(sc, **f)
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["occurred_at", "type", "client_name", "broker", "masked_account", "symbol", "exchange", "side",
                "order_type", "quantity", "filled_quantity", "price", "average_price", "status", "order_id", "trade_id",
                "trading_mode", "source", "message"]
        w.writerow(cols)
        for r in rows:
            w.writerow([_csv_safe(r.get(c)) for c in cols])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="tradepulse-events-{today_ist()}.csv"'})

    @app.get("/api/events/stream")
    async def event_stream(request: Request, sc: Scope = Depends(scope)):
        q = hub.subscribe(set(sc.account_ids))

        async def gen():
            try:
                yield f"event: hello\ndata: {json.dumps({'at': ist_iso(), 'accounts': len(sc.account_ids), 'build': build_id()})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(q.get(), timeout=15)
                        data["masked_account"] = mask_account(data.get("account_id"))
                        yield f"event: trade\ndata: {json.dumps(data, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/events/{event_id}")
    def event(event_id: str, sc: Scope = Depends(scope)):
        e = rt.svc.event(sc, event_id)
        if not e:
            raise HTTPException(404, "Event not found")
        return e

    # --- groups -----------------------------------------------------------------------------------
    @app.get("/api/groups")
    def groups(u: dict = Depends(user)):
        visible = set(rt.oms.visible_account_ids(u))
        gs = rt.store.groups()
        if u["role"] != "ADMIN":
            gs = [g for g in gs if g["account_ids"] and set(g["account_ids"]) <= visible]
        return gs

    def _save_group(body: GroupIn, u: dict, group_id: int | None = None) -> dict:
        known = set(rt.oms.visible_account_ids(u))
        bad = [a for a in body.account_ids if a not in known]
        if bad:
            raise HTTPException(400, f"Unknown account(s): {', '.join(bad[:5])}")
        chans = {c["id"] for c in rt.store.channels()}
        if any(c not in chans for c in body.channel_ids):
            raise HTTPException(400, "Unknown channel id")
        bad_rules = [k for k in body.rules if k not in EVENT_TYPES and k not in CHANNEL_KEYS.values()]
        if bad_rules:
            raise HTTPException(400, f"Unknown event type(s): {', '.join(bad_rules)}")
        now = ist_iso()
        modes = body.modes
        if modes is None:                                  # not sent: keep the stored value (new group: global)
            existing = rt.store.group(group_id) if group_id is not None else None
            modes = (existing or {}).get("modes") or []
        try:
            with rt.store.tx() as c:
                vals = (body.name.strip(), body.description, int(body.enabled), body.color, body.delivery_mode,
                        body.batch_minutes, body.digest_interval_min, json.dumps(body.digest_times),
                        body.position_interval_min, int(body.eod_summary), int(body.allow_queries), json.dumps(modes))
                if group_id is None:
                    group_id = c.execute("""INSERT INTO groups (name, description, enabled, color, delivery_mode,
                        batch_minutes, digest_interval_min, digest_times, position_interval_min, eod_summary,
                        allow_queries, modes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                         (*vals, now, now)).lastrowid
                else:
                    if not c.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,)).fetchone():
                        raise HTTPException(404, "Group not found")
                    c.execute("""UPDATE groups SET name=?, description=?, enabled=?, color=?, delivery_mode=?,
                        batch_minutes=?, digest_interval_min=?, digest_times=?, position_interval_min=?, eod_summary=?,
                        allow_queries=?, modes=?, updated_at=? WHERE id=?""", (*vals, now, group_id))
                c.execute("DELETE FROM group_accounts WHERE group_id = ?", (group_id,))
                c.executemany("INSERT INTO group_accounts (group_id, account_id) VALUES (?, ?)",
                              [(group_id, a) for a in sorted(set(body.account_ids))])
                c.execute("DELETE FROM group_channels WHERE group_id = ?", (group_id,))
                c.executemany("INSERT INTO group_channels (group_id, channel_id) VALUES (?, ?)",
                              [(group_id, ch) for ch in sorted(set(body.channel_ids))])
                c.execute("DELETE FROM rules WHERE scope = 'group' AND scope_id = ?", (str(group_id),))
                c.executemany("INSERT INTO rules (scope, scope_id, event_type, enabled) VALUES ('group', ?, ?, ?)",
                              [(str(group_id), k, int(v)) for k, v in body.rules.items() if v is not None])
        except HTTPException:
            raise
        except Exception as ex:
            if "UNIQUE" in str(ex):
                raise HTTPException(409, f"A group named {body.name!r} already exists.")
            raise
        changed(u, "GROUP_SAVED", body.name, {"accounts": len(body.account_ids), "channels": body.channel_ids})
        return rt.store.group(group_id)

    @app.post("/api/groups")
    def create_group(body: GroupIn, u: dict = Depends(admin)):
        return _save_group(body, u)

    @app.put("/api/groups/{group_id}")
    def update_group(group_id: int, body: GroupIn, u: dict = Depends(admin)):
        return _save_group(body, u, group_id)

    @app.delete("/api/groups/{group_id}")
    def delete_group(group_id: int, u: dict = Depends(admin)):
        g = rt.store.group(group_id)
        if not g:
            raise HTTPException(404, "Group not found")
        with rt.store.tx() as c:
            c.execute("DELETE FROM groups WHERE id = ?", (group_id,))
            c.execute("DELETE FROM rules WHERE scope = 'group' AND scope_id = ?", (str(group_id),))
        changed(u, "GROUP_DELETED", g["name"])
        return {"ok": True}

    @app.post("/api/groups/{group_id}/send-now")
    def group_send_now(group_id: int, kind: str = "digest", u: dict = Depends(admin)):
        g = rt.store.group(group_id)
        if not g:
            raise HTTPException(404, "Group not found")
        if kind not in ("digest", "positions", "eod"):
            raise HTTPException(400, "kind must be digest, positions or eod")
        n = rt.scheduler.send_now(g, kind)
        rt.store.audit(u["username"], "GROUP_REPORT_SENT", g["name"], kind)
        return {"queued": n}

    # --- notification rules ------------------------------------------------------------------------
    @app.get("/api/notifications/settings")
    def notification_settings(sc: Scope = Depends(scope)):
        accts = rt.svc.accounts(sc)
        rules = rt.store.all_rules()
        return {
            "event_types": [{"type": et, "label": EVENT_LABELS[et], "default": DEFAULT_ENABLED[et]} for et in EVENT_TYPES],
            "channel_keys": CHANNEL_KEYS,
            "global": rt.store.rules_for("global", ""),
            "clients": {str(cid): rt.store.rules_for("client", str(cid)) for cid in {a["user_id"] for a in accts}},
            "accounts": {a["account_id"]: rt.store.rules_for("account", a["account_id"]) for a in accts},
            "groups": {str(g["id"]): g["rules"] for g in rt.store.groups()},
            "count": len(rules),
            "precedence": ["account", "client", "group", "global", "default"],
            "settings": {k: rt.store.setting(k) for k in ("telegram_enabled", "whatsapp_enabled", "notify_modes")},
        }

    @app.put("/api/notifications/settings")
    def put_notification_settings(body: RulesIn, u: dict = Depends(admin)):
        bad = [k for k in body.rules if k not in EVENT_TYPES and k not in CHANNEL_KEYS.values()]
        if bad:
            raise HTTPException(400, f"Unknown event type(s): {', '.join(bad)}")
        if body.scope == "global":
            body.scope_id = ""
        elif body.scope == "account":
            _need(rt.svc.scope_for_user(u), body.scope_id)
        elif body.scope == "group" and not rt.store.group(int(body.scope_id or 0)):
            raise HTTPException(404, "Group not found")
        rt.store.set_rules(body.scope, body.scope_id, body.rules)
        changed(u, "RULES_UPDATED", f"{body.scope}:{body.scope_id}", body.rules)
        return {"ok": True, "rules": rt.store.rules_for(body.scope, body.scope_id)}

    @app.get("/api/notifications/explain")
    def notification_explain(account_id: str, event_type: str, sc: Scope = Depends(scope)):
        _need(sc, account_id)
        if event_type not in EVENT_TYPES:
            raise HTTPException(400, "Unknown event type")
        a = rt.oms.account(account_id)
        snap = RuleSnapshot.load(rt.store, {account_id: a["user_id"]} if a else {})
        return {"account_id": account_id, "event_type": event_type,
                "monitored": bool((snap.monitored.get(account_id) or {}).get("enabled")),
                "muted": bool((snap.monitored.get(account_id) or {}).get("muted")),
                "groups": explain(snap, event_type, account_id)}

    @app.get("/api/notifications/history")
    def notification_history(status: str | None = None, limit: int = 200, sc: Scope = Depends(scope),
                             u: dict = Depends(user)):
        where, p = "", []
        if status:
            where = "WHERE n.status = ?"
            p.append(status.upper())
        rows = rt.store.rows(f"""SELECT n.id, n.event_id, n.kind, n.status, n.attempts, n.last_error, n.created_at,
                                       n.sent_at, n.next_attempt_at, n.batched, c.title AS channel, c.kind AS provider,
                                       g.name AS group_name, e.type AS event_type, e.account_id, e.symbol, e.client_name
                                  FROM notifications n JOIN channels c ON c.id = n.channel_id
                                  LEFT JOIN groups g ON g.id = n.group_id
                                  LEFT JOIN events e ON e.event_id = n.event_id
                                  {where} ORDER BY n.id DESC LIMIT ?""", (*p, min(limit, 2000)))
        if u["role"] != "ADMIN":
            rows = [r for r in rows if r["account_id"] and sc.allows(r["account_id"])]
        for r in rows:
            r["masked_account"] = mask_account(r.pop("account_id") or "")
        return rows

    @app.post("/api/notifications/test")
    def notification_test(body: TestIn, u: dict = Depends(admin)):
        ch = rt.store.one("SELECT * FROM channels WHERE id = ?", (body.channel_id,))
        if not ch:
            raise HTTPException(404, "Channel not found")
        text = (f"🔔 <b>TradePulse test message</b>\n\n{h(body.text) if body.text else 'Notifications to this chat are working.'}"
                f"\n\nSent by {h(u['name'])} · {h(ist_iso()[11:19])} IST")
        if ch["status"] != "ACTIVE" or not ch["enabled"]:
            raise HTTPException(409, "Activate and enable this chat before sending a test.")
        nid = queue_message(rt.store, ch["id"], text, kind="test")
        rt.store.audit(u["username"], "TEST_NOTIFICATION", ch["title"] or ch["chat_id"])
        return {"queued": nid}

    @app.post("/api/notifications/{notification_id}/retry")
    def notification_retry(notification_id: int, u: dict = Depends(admin)):
        n = rt.store.execute("UPDATE notifications SET status = 'QUEUED', attempts = 0, next_attempt_at = ? "
                             "WHERE id = ? AND status IN ('DEAD','FAILED','SKIPPED')", (ist_iso(), notification_id))
        rt.store.audit(u["username"], "NOTIFICATION_RETRY", str(notification_id))
        return {"requeued": n}

    @app.post("/api/notifications/retry-dead")
    def notification_retry_dead(u: dict = Depends(admin)):
        n = rt.store.execute("UPDATE notifications SET status = 'QUEUED', attempts = 0, next_attempt_at = ? "
                             "WHERE status = 'DEAD'", (ist_iso(),))
        rt.store.audit(u["username"], "NOTIFICATION_RETRY_DEAD", str(n))
        return {"requeued": n}

    # --- Telegram -----------------------------------------------------------------------------------
    @app.get("/api/telegram")
    def telegram_info(u: dict = Depends(user)):
        bot_health = _health_component("telegram_bot")
        out = {"configured": bool(rt.bot_token()), "from_env": bool(settings.TELEGRAM_BOT_TOKEN),
               "token": rt.vault.describe("telegram_bot_token") if not settings.TELEGRAM_BOT_TOKEN else {"set": True, "hint": "env"},
               "mode": settings.TELEGRAM_MODE, "username": rt.bot.username or (bot_health or {}).get("username", ""),
               "health": bot_health}
        if u["role"] == "ADMIN":
            out["chats"] = rt.store.channels()
            out["users"] = rt.store.rows("""SELECT telegram_user_id, username, first_name, oms_user_id, linked_at, last_seen,
                                                   blocked FROM tg_users ORDER BY last_seen DESC LIMIT 500""")
            names = {x["id"]: x for x in rt.oms.users()}
            for tu in out["users"]:
                ou = names.get(tu["oms_user_id"]) if tu["oms_user_id"] else None
                tu["oms_user"] = {"name": ou["name"], "role": ou["role"], "username": ou["username"]} if ou else None
        return out

    @app.put("/api/telegram/token")
    def telegram_token(body: TokenIn, u: dict = Depends(admin)):
        if settings.TELEGRAM_BOT_TOKEN:
            raise HTTPException(409, "The bot token is set by TP_TELEGRAM_BOT_TOKEN in the environment.")
        token = body.token.strip()
        if token and not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,60}", token):
            raise HTTPException(400, "That doesn't look like a BotFather token (123456789:ABC...).")
        bot = None
        if token:
            from .notify.telegram import TelegramClient
            try:
                bot = TelegramClient(token, settings.TELEGRAM_API).get_me()
            except Exception as ex:
                raise HTTPException(400, f"Telegram rejected the token: {getattr(ex, 'description', ex)}")
        rt.vault.put("telegram_bot_token", token)
        rt.bot.username = (bot or {}).get("username", "")
        rt.store.audit(u["username"], "TELEGRAM_TOKEN_SET" if token else "TELEGRAM_TOKEN_CLEARED")
        return {"ok": True, "bot": bot}

    @app.post("/api/telegram/chats")
    def telegram_add_chat(body: ChatIn, u: dict = Depends(admin)):
        ch = rt.store.upsert_channel(body.kind, body.chat_id, title=body.title,
                                     chat_type="whatsapp" if body.kind == "whatsapp" else "", enabled=True, status="ACTIVE")
        changed(u, "CHAT_ADDED", body.title or body.chat_id, body.kind)
        return ch

    @app.put("/api/telegram/chats/{channel_id}")
    def telegram_update_chat(channel_id: int, body: ChatUpdate, u: dict = Depends(admin)):
        ch = rt.store.one("SELECT * FROM channels WHERE id = ?", (channel_id,))
        if not ch:
            raise HTTPException(404, "Chat not found")
        with rt.store.tx() as c:
            if body.enabled is not None:
                c.execute("UPDATE channels SET enabled = ? WHERE id = ?", (int(body.enabled), channel_id))
            if body.status is not None:
                c.execute("UPDATE channels SET status = ? WHERE id = ?", (body.status, channel_id))
            if body.title is not None:
                c.execute("UPDATE channels SET title = ? WHERE id = ?", (body.title, channel_id))
            if body.group_ids is not None:
                valid = {g["id"] for g in rt.store.groups()}
                c.execute("DELETE FROM group_channels WHERE channel_id = ?", (channel_id,))
                c.executemany("INSERT INTO group_channels (group_id, channel_id) VALUES (?, ?)",
                              [(g, channel_id) for g in body.group_ids if g in valid])
        changed(u, "CHAT_UPDATED", ch["title"] or ch["chat_id"], body.model_dump(exclude_none=True))
        return rt.store.one("SELECT * FROM channels WHERE id = ?", (channel_id,))

    @app.delete("/api/telegram/chats/{channel_id}")
    def telegram_delete_chat(channel_id: int, u: dict = Depends(admin)):
        ch = rt.store.one("SELECT * FROM channels WHERE id = ?", (channel_id,))
        if not ch:
            raise HTTPException(404, "Chat not found")
        rt.store.delete_channel(channel_id)          # really gone from the list (it used to be only disabled)
        changed(u, "CHAT_REMOVED", ch["title"] or ch["chat_id"])
        return {"ok": True}

    @app.post("/api/telegram/link-code")
    def telegram_link_code(u: dict = Depends(user)):
        if not limiter.hit(f"linkcode:{u['id']}", 5):
            raise HTTPException(429, "Too many codes - wait a minute.")
        out = create_link_code(rt.store, u["id"])
        out["bot_username"] = rt.bot.username
        return out

    @app.delete("/api/telegram/users/{telegram_user_id}")
    def telegram_unlink(telegram_user_id: str, u: dict = Depends(admin)):
        rt.store.execute("UPDATE tg_users SET oms_user_id = NULL, linked_at = '' WHERE telegram_user_id = ?",
                         (telegram_user_id,))
        rt.store.audit(u["username"], "TELEGRAM_USER_UNLINKED", telegram_user_id)
        return {"ok": True}

    @app.post("/api/telegram/webhook/{secret}")
    async def telegram_webhook(secret: str, request: Request):
        want = rt.vault.get("telegram_webhook_secret")
        got = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not want or not secrets.compare_digest(secret, want) or not secrets.compare_digest(got, want):
            raise HTTPException(404, "Not found")
        update = await request.json()
        threading.Thread(target=rt.bot.handle_update, args=(update,), daemon=True).start()
        return {"ok": True}

    @app.post("/api/telegram/webhook-mode")
    def telegram_webhook_mode(enable: bool, u: dict = Depends(admin)):
        tg = rt.telegram()
        if tg is None:
            raise HTTPException(409, "Configure the bot token first.")
        if enable:
            if not settings.PUBLIC_BASE_URL.startswith("https://"):
                raise HTTPException(409, "Set TP_PUBLIC_BASE_URL to the public https:// address first.")
            secret = secrets.token_urlsafe(24).replace("-", "").replace("_", "")
            rt.vault.put("telegram_webhook_secret", secret)
            tg.set_webhook(f"{settings.PUBLIC_BASE_URL}/api/telegram/webhook/{secret}", secret)
        else:
            tg.delete_webhook()
        rt.store.audit(u["username"], "TELEGRAM_WEBHOOK_" + ("ON" if enable else "OFF"))
        return {"ok": True}

    # --- WhatsApp (optional) --------------------------------------------------------------------------
    @app.put("/api/whatsapp/config")
    def whatsapp_config(body: WhatsAppIn, u: dict = Depends(admin)):
        if body.token:
            rt.vault.put("whatsapp_token", body.token.strip())
        if body.phone_number_id:
            rt.vault.put("whatsapp_phone_number_id", body.phone_number_id.strip())
        rt.store.put_settings({"whatsapp_template": body.template.strip()})
        rt.store.audit(u["username"], "WHATSAPP_CONFIGURED")
        return {"configured": rt.whatsapp.configured()}

    @app.get("/api/whatsapp")
    def whatsapp_info(u: dict = Depends(admin)):
        return {"configured": rt.whatsapp.configured(), "token": rt.vault.describe("whatsapp_token"),
                "phone_number_id": rt.vault.describe("whatsapp_phone_number_id"),
                "template": rt.store.setting("whatsapp_template") or "",
                "enabled": rt.store.setting("whatsapp_enabled")}

    # --- broker webhooks ---------------------------------------------------------------------------------
    @app.api_route("/api/webhooks/brokers/{broker}", methods=["GET", "POST"])
    async def broker_webhook(broker: str, request: Request):
        broker = broker.lower()
        adapter = rt.adapters.get(broker)
        if adapter is None:
            raise HTTPException(404, "Unknown broker")
        if request.method == "GET":                     # brokers probe the URL when it is registered
            return {"ok": True, "broker": broker, "supported": adapter.supported}
        ip = request.client.host if request.client else "?"
        if not limiter.hit(f"wh:{broker}:{ip}", settings.WEBHOOK_RATE_PER_MIN):
            _wh_log(broker, "RATE_LIMITED", "", ip)
            raise HTTPException(429, "rate limited")
        raw = await request.body()
        if len(raw) > 65536:
            _wh_log(broker, "REJECTED", "", "body too large")
            raise HTTPException(413, "body too large")
        try:
            evs = adapter.handle(raw, {k.lower(): v for k, v in request.headers.items()}, dict(request.query_params))
        except WebhookRejected as ex:
            _wh_log(broker, "REJECTED", "", f"{ex.status} {ex.reason}")
            raise HTTPException(ex.status, ex.reason)
        for e in evs:
            enqueue(rt.bus, e)
        _wh_log(broker, "ACCEPTED" if evs else "IGNORED", evs[0]["account_id"] if evs else "",
                ",".join(e["type"] for e in evs))
        return {"ok": True, "accepted": len(evs)}

    @app.get("/api/webhooks")
    def webhooks_info(request: Request, u: dict = Depends(user)):
        base = settings.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
        out = []
        for name, a in rt.adapters.items():
            d = a.describe()
            d["url"] = f"{base}/api/webhooks/brokers/{name}" + ("?token=<angel token>" if name == "angel" else "")
            last = rt.store.one("SELECT at, outcome, detail FROM webhook_log WHERE broker = ? ORDER BY id DESC LIMIT 1", (name,))
            counts = {r["outcome"]: r["n"] for r in rt.store.rows(
                "SELECT outcome, COUNT(*) AS n FROM webhook_log WHERE broker = ? AND substr(at,1,10) = ? GROUP BY outcome",
                (name, today_ist()))}
            d.update(last=last, today=counts)
            out.append(d)
        return out

    @app.put("/api/webhooks/{broker}/secret")
    def webhook_secret(broker: str, body: SecretIn, u: dict = Depends(admin)):
        if broker not in ("zerodha", "angel", "generic"):
            raise HTTPException(400, "Secrets apply to zerodha, angel and generic")
        name = f"webhook:{broker}" + (f":{body.account_id}" if body.account_id and broker == "zerodha" else "")
        rt.vault.put(name, body.secret.strip())
        rt.store.audit(u["username"], "WEBHOOK_SECRET_SET", name)
        return {"ok": True, "secret": rt.vault.describe(name)}

    @app.get("/api/webhooks/log")
    def webhooks_log(broker: str | None = None, u: dict = Depends(admin)):
        if broker:
            return rt.store.rows("SELECT * FROM webhook_log WHERE broker = ? ORDER BY id DESC LIMIT 200", (broker,))
        return rt.store.rows("SELECT * FROM webhook_log ORDER BY id DESC LIMIT 200")

    def _wh_log(broker: str, outcome: str, account_id: str, detail: str) -> None:
        try:
            rt.store.execute("INSERT INTO webhook_log (broker, at, outcome, account_id, detail) VALUES (?, ?, ?, ?, ?)",
                             (broker, ist_iso(), outcome, account_id, detail[:500]))
            rt.bus.hset(busmod.HEALTH, f"webhook:{broker}", json.dumps({"at": ist_iso(), "outcome": outcome}))
        except Exception:
            pass

    # --- settings / health / audit ---------------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings(u: dict = Depends(user)):
        return {"values": rt.store.get_settings(), "defaults": DEFAULT_SETTINGS}

    @app.put("/api/settings")
    def put_settings(body: dict, u: dict = Depends(admin)):
        clean = {}
        for k, v in body.items():
            if k not in DEFAULT_SETTINGS:
                raise HTTPException(400, f"Unknown setting {k!r}")
            if k in SETTINGS_RULES:
                _t, lo, hi = SETTINGS_RULES[k]
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{k} must be a number")
                if not lo <= v <= hi:
                    raise HTTPException(400, f"{k} must be between {lo} and {hi}")
            elif k in ("market_open", "market_close", "eod_summary_time"):
                if not isinstance(v, str) or not HHMM.match(v):
                    raise HTTPException(400, f"{k} must be HH:MM")
            elif k == "notify_modes":
                if not isinstance(v, list) or any(m not in ("LIVE", "PAPER", "DRY_RUN") for m in v):
                    raise HTTPException(400, "notify_modes must be a list of LIVE / PAPER / DRY_RUN")
            elif k == "trading_days":
                if not isinstance(v, list) or any(not isinstance(d, int) or not 0 <= d <= 6 for d in v):
                    raise HTTPException(400, "trading_days must be weekday numbers 0-6")
            elif k == "reconcile_times":
                if not isinstance(v, list) or len(v) > 6 or any(not isinstance(t, str) or not HHMM.match(t) for t in v):
                    raise HTTPException(400, "reconcile_times must be up to 6 HH:MM times")
                v = sorted(set(v))
            elif isinstance(DEFAULT_SETTINGS[k], bool):
                v = bool(v)
            clean[k] = v
        rt.store.put_settings(clean)
        changed(u, "SETTINGS_UPDATED", "", clean)
        return {"values": rt.store.get_settings()}

    def _live(name: str, local: dict, comps: dict | None = None) -> dict:
        """Status of a background component. In production the workers run in tradepulse-worker, so this
        process's own counters are idle; the worker publishes its real status to the Redis health hash."""
        if run_workers:
            return local
        comps = comps if comps is not None else {k: _safe_json(v) for k, v in rt.bus.hgetall(busmod.HEALTH).items()}
        return comps.get(name) or {k: (v if isinstance(v, str) else None) for k, v in local.items()}

    @app.get("/api/system/health")
    def system_health(u: dict = Depends(user)):
        comps = {k: _safe_json(v) for k, v in rt.bus.hgetall(busmod.HEALTH).items()}
        q = {r["status"]: r["n"] for r in rt.store.rows("SELECT status, COUNT(*) AS n FROM notifications GROUP BY status")}
        tail = _live("redis_tail", rt.tail.stats, comps)
        out = {
            "version": __version__, "at": ist_iso(),
            "redis": {"kind": rt.bus.kind, "ok": rt.bus.ping()},
            "oms_link": "redis-only",
            "ingest": {"pending": rt.bus.pending_count(busmod.INGEST, busmod.INGEST_GROUP),
                       "length": rt.bus.length(busmod.INGEST)},
            "processor": {"processed": 0, "duplicates": 0, "failed": 0, **_live("processor", rt.processor.stats, comps)},
            "redis_tail": {"messages": 0, "streams": 0, "roster_accounts": 0, "errors": 0, **tail},
            "notifier": _live("notifier", rt.notifier.stats, comps),
            "brokers": {**rt.readers.stats, "slot_seconds": rt.readers.slot_ms() // 1000},
            "reconcile": {"running": rt.reconciler.current or (comps.get("reconciler") or {}).get("running"),
                          "last": rt.store.one("SELECT * FROM reconcile_runs ORDER BY id DESC LIMIT 1"),
                          "times": rt.store.setting("reconcile_times")},
            "notifications": q,
            "telegram": {"updates": 0, "replies": 0, **_live("telegram_bot", rt.bot.stats, comps)},
            "vault": {"generated_key": rt.vault.generated_key}, "components": comps,
            "cursors": {r["name"]: r["value"] for r in rt.store.rows("SELECT * FROM cursors")} if u["role"] == "ADMIN" else {},
            "no_broker_polling": True,
        }
        return out

    def _health_brief() -> dict:
        tail = _live("redis_tail", rt.tail.stats)
        return {"oms_stream": bool(tail.get("redis_ok")), "oms_last_message": tail.get("last_message_at"),
                "redis": rt.bus.kind == "redis" and rt.bus.ping(),
                "reconcile": rt.store.one("SELECT trigger, status, finished_at, mismatches FROM reconcile_runs "
                                          "WHERE trigger LIKE 'SCHEDULED%' ORDER BY id DESC LIMIT 1"),
                "telegram": bool(rt.bot_token()), "bot_ok": (_health_component("telegram_bot") or {}).get("ok"),
                "last_tick": tail.get("last_tick"), "queue": rt.store.one(
                    "SELECT COUNT(*) AS n FROM notifications WHERE status IN ('QUEUED','FAILED')")["n"],
                "dead": rt.store.one("SELECT COUNT(*) AS n FROM notifications WHERE status = 'DEAD'")["n"]}

    def _health_component(name: str):
        return _safe_json(rt.bus.hgetall(busmod.HEALTH).get(name))

    @app.get("/api/audit")
    def audit(limit: int = 300, u: dict = Depends(admin)):
        return rt.store.rows("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(limit, 2000),))

    # --- holdings & reconciliation ---------------------------------------------------------------------
    @app.get("/api/holdings")
    def holdings(account_id: str | None = None, sc: Scope = Depends(scope)):
        if account_id:
            _need(sc, account_id)
        return [_clean(r) for r in rt.svc.holdings(sc, account_id)]

    @app.get("/api/reconcile")
    def reconcile_status(sc: Scope = Depends(scope)):
        out = rt.svc.reconcile_status(sc)
        out["running"] = rt.reconciler.current or (_health_component("reconciler") or {}).get("running")
        out["slot_seconds"] = rt.readers.slot_ms() // 1000
        out["times"] = rt.store.setting("reconcile_times")
        return out

    @app.post("/api/reconcile/request")
    def reconcile_request(body: RefreshIn, u: dict = Depends(user), sc: Scope = Depends(scope)):
        """One read-only broker request for one account (positions OR holdings). Global slot: 1 per 3 s."""
        from .brokers import BrokerReadError, Busy
        _need(sc, body.account_id)
        try:
            res = rt.reconciler.request(body.account_id, body.kind, u["username"])
        except Busy as b:
            secs = max(1, -(-b.wait_ms // 1000))
            return JSONResponse({"detail": f"Another broker request is in progress. Please wait {secs} second"
                                           f"{'s' if secs != 1 else ''} and try again (1 request every "
                                           f"{rt.readers.slot_ms() // 1000} seconds).", "wait_ms": b.wait_ms},
                                status_code=429, headers={"Retry-After": str(secs)})
        except BrokerReadError as ex:
            raise HTTPException(502, f"Could not read {body.kind} from the broker: {ex}")
        rt.store.audit(u["username"], "BROKER_REFRESH", body.account_id, body.kind)
        return {"ok": True, "rows": res["rows"], "mismatches": res["mismatches"], "next_allowed_ms": rt.readers.slot_ms()}

    @app.post("/api/reconcile/run")
    def reconcile_run(u: dict = Depends(admin)):
        """Full pass now (monitored accounts), in the background - it waits for the 3 s slot per request."""
        if rt.reconciler.current:
            raise HTTPException(409, "A reconcile is already running.")
        n = len(rt.reconciler.accounts_for_run())
        threading.Thread(target=rt.reconciler.run, args=(f"USER:{u['username']} full",), daemon=True).start()
        rt.store.audit(u["username"], "RECONCILE_RUN")
        return {"started": True, "accounts": n, "estimated_seconds": n * 2 * rt.readers.slot_ms() // 1000}

    # --- users (TradePulse's own directory) --------------------------------------------------------------
    @app.get("/api/users")
    def users_list(u: dict = Depends(admin)):
        rows = rt.store.rows("SELECT id, username, name, role, status, manager_id, source, created_at, updated_at FROM users ORDER BY name")
        accts = rt.store.rows("SELECT account_id, user_id, broker FROM accounts")
        for r in rows:
            r["accounts"] = [{"account_id": a["account_id"], "masked_account": mask_account(a["account_id"]), "broker": a["broker"]}
                             for a in accts if a["user_id"] == r["id"]]
        return rows

    @app.post("/api/users")
    def users_create(body: UserIn, u: dict = Depends(admin)):
        from .users import create_user
        if not body.password:
            raise HTTPException(400, "Set an initial password (at least 8 characters).")
        try:
            uid = create_user(rt.store, body.username, body.name, body.role, body.password, body.manager_id)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        except Exception as ex:
            if "UNIQUE" in str(ex):
                raise HTTPException(409, f"Username {body.username!r} is taken.")
            raise
        _set_user_accounts(uid, body.account_ids)
        rt.store.audit(u["username"], "USER_CREATED", body.username, body.role)
        return {"id": uid}

    @app.put("/api/users/{user_id}")
    def users_update(user_id: int, body: UserIn, u: dict = Depends(admin)):
        from .users import hash_password
        if not rt.store.one("SELECT 1 AS x FROM users WHERE id = ?", (user_id,)):
            raise HTTPException(404, "User not found")
        if user_id == u["id"] and (body.role != "ADMIN" or body.status != "ACTIVE"):
            raise HTTPException(400, "You cannot demote or disable your own account.")
        if body.password and len(body.password) < 8:
            raise HTTPException(400, "Password must be at least 8 characters.")
        with rt.store.tx() as c:
            c.execute("UPDATE users SET name = ?, role = ?, status = ?, manager_id = ?, updated_at = ? WHERE id = ?",
                      (body.name.strip(), body.role, body.status, body.manager_id, ist_iso(), user_id))
            if body.password:
                c.execute("UPDATE users SET auth_hash = ? WHERE id = ?", (hash_password(body.password), user_id))
                c.execute("DELETE FROM sessions WHERE oms_user_id = ?", (user_id,))
            if body.status != "ACTIVE":
                c.execute("DELETE FROM sessions WHERE oms_user_id = ?", (user_id,))
        _set_user_accounts(user_id, body.account_ids)
        rt.store.audit(u["username"], "USER_UPDATED", body.username, {"role": body.role, "status": body.status,
                                                                         "password_reset": bool(body.password)})
        return {"ok": True}

    def _set_user_accounts(user_id: int, account_ids: list[str] | None) -> None:
        if account_ids is None:
            return
        with rt.store.tx() as c:
            c.execute("UPDATE accounts SET user_id = NULL WHERE user_id = ?", (user_id,))
            for a in account_ids:
                c.execute("UPDATE accounts SET user_id = ? WHERE account_id = ?", (user_id, a))

    @app.get("/api/meta")
    def meta(u: dict = Depends(user)):
        return {"event_types": [{"type": et, "label": EVENT_LABELS[et]} for et in EVENT_TYPES],
                "brokers": sorted({a["broker"] for a in rt.oms.accounts()}), "version": __version__,
                "today": today_ist(), "build": build_id()}

    return app


def build_id() -> str:
    """Identifies the deployed dashboard code; an open tab compares it to detect that it is out of date."""
    try:
        return str(int(max((WEB_DIR / f).stat().st_mtime for f in ("app.js", "index.html", "styles.css"))))
    except OSError:
        return __version__


# --- helpers ---------------------------------------------------------------------------------------
def _day(day: str | None) -> str:
    if day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return day
    return today_ist()


def _need(sc: Scope, account_id: str) -> None:
    if not sc.allows(account_id):
        raise HTTPException(404, "Account not found")


def _ids(sc: Scope, account_id: str | None) -> list[str]:
    if account_id:
        ids = [a for a in account_id.split(",") if a]
        for a in ids:
            _need(sc, a)
        return ids
    return sc.account_ids


def _acct(a: dict) -> dict:
    keep = ("account_id", "masked_account", "broker", "account_status", "auth_method", "user_id", "username",
            "client_name", "manager_name", "session_status", "session_updated", "trading_mode", "automation_enabled",
            "worker_status", "worker_heartbeat", "broker_connected", "open_orders", "monitored", "muted", "groups",
            "user_status")
    return {k: a.get(k) for k in keep}


def _event_filters(request: Request) -> dict:
    qp = request.query_params
    types = [t for t in (qp.get("types") or "").split(",") if t in EVENT_TYPES] or None
    try:
        limit = min(int(qp.get("limit") or 200), 2000)
        before = int(qp["before_id"]) if qp.get("before_id") else None
    except ValueError:
        raise HTTPException(400, "limit/before_id must be numbers")
    return {"account_id": qp.get("account_id") or None, "types": types, "broker": qp.get("broker") or None,
            "day": qp.get("day") or None, "date_from": qp.get("date_from") or None, "date_to": qp.get("date_to") or None,
            "search": (qp.get("search") or "")[:64] or None, "client": (qp.get("client") or "")[:64] or None,
            "before_id": before, "limit": limit}


def _csv_safe(v):
    """Neutralise spreadsheet formula injection in exported cells."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return "" if v is None else v


def _clean(obj):
    """Drop Decimal helper fields and make the payload JSON-safe."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if not k.endswith("_d")}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _safe_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def main() -> None:
    import uvicorn

    from .config import settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(settings), host=settings.HOST, port=settings.PORT, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
