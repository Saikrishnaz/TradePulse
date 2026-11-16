"""TradePulse's own SQLite database: users, accounts, orders/fills (copied from the OMS's Redis
streams), the position & holdings book, configuration, the normalized event store, the notification
outbox, Telegram identities and the audit log. TradePulse never connects to the OMS database or API:
everything here was learnt from Redis (or a read-only broker snapshot), so it keeps working after the
OMS host goes down for the day.

The outbox is what makes notifications durable: a notification row is inserted in the same
transaction as its event, and only marked SENT after Telegram accepts it, so a crash or restart
at any point resends rather than loses. ``UNIQUE(event_id, channel_id)`` makes a redelivered
event unable to create a second notification.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import ist_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    oms_user_id INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    ip          TEXT NOT NULL DEFAULT '',
    user_agent  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS monitored_accounts (
    account_id TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    muted      INTEGER NOT NULL DEFAULT 0,
    label      TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL UNIQUE,
    description          TEXT NOT NULL DEFAULT '',
    enabled              INTEGER NOT NULL DEFAULT 1,
    color                TEXT NOT NULL DEFAULT '#3b82f6',
    delivery_mode        TEXT NOT NULL DEFAULT 'instant' CHECK (delivery_mode IN ('instant','batched')),
    batch_minutes        INTEGER NOT NULL DEFAULT 5,
    digest_interval_min  INTEGER NOT NULL DEFAULT 0,
    digest_times         TEXT NOT NULL DEFAULT '[]',
    position_interval_min INTEGER NOT NULL DEFAULT 0,
    eod_summary          INTEGER NOT NULL DEFAULT 1,
    allow_queries        INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_accounts (
    group_id   INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    PRIMARY KEY (group_id, account_id)
);
CREATE TABLE IF NOT EXISTS channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL DEFAULT 'telegram' CHECK (kind IN ('telegram','whatsapp')),
    chat_id    TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    chat_type  TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'PENDING',
    last_seen  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (kind, chat_id)
);
CREATE TABLE IF NOT EXISTS group_channels (
    group_id   INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, channel_id)
);
-- Event-type rules. scope: global | client | account | group. value: 1 on, 0 off (absent = inherit).
CREATE TABLE IF NOT EXISTS rules (
    scope      TEXT NOT NULL CHECK (scope IN ('global','client','account','group')),
    scope_id   TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    enabled    INTEGER NOT NULL,
    PRIMARY KEY (scope, scope_id, event_type)
);
CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL UNIQUE,
    dedupe_key       TEXT NOT NULL UNIQUE,
    type             TEXT NOT NULL,
    source           TEXT NOT NULL,
    account_id       TEXT NOT NULL DEFAULT '',
    client_name      TEXT NOT NULL DEFAULT '',
    broker           TEXT NOT NULL DEFAULT '',
    trading_mode     TEXT NOT NULL DEFAULT '',
    symbol           TEXT NOT NULL DEFAULT '',
    exchange         TEXT NOT NULL DEFAULT '',
    side             TEXT NOT NULL DEFAULT '',
    order_type       TEXT NOT NULL DEFAULT '',
    product          TEXT NOT NULL DEFAULT '',
    quantity         INTEGER NOT NULL DEFAULT 0,
    filled_quantity  INTEGER NOT NULL DEFAULT 0,
    price            REAL,
    average_price    REAL,
    status           TEXT NOT NULL DEFAULT '',
    order_id         TEXT NOT NULL DEFAULT '',
    internal_order_id TEXT NOT NULL DEFAULT '',
    trade_id         TEXT NOT NULL DEFAULT '',
    message          TEXT NOT NULL DEFAULT '',
    payload          TEXT NOT NULL DEFAULT '{}',
    occurred_at      TEXT NOT NULL,
    received_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id, id);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT,
    kind            TEXT NOT NULL DEFAULT 'event',
    channel_id      INTEGER NOT NULL,
    group_id        INTEGER,
    text            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','SENT','FAILED','DEAD','SKIPPED')),
    batched         INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT NOT NULL DEFAULT '',
    provider_ref    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    sent_at         TEXT NOT NULL DEFAULT '',
    UNIQUE (event_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_notifications_due ON notifications(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS tg_users (
    telegram_user_id TEXT PRIMARY KEY,
    username         TEXT NOT NULL DEFAULT '',
    first_name       TEXT NOT NULL DEFAULT '',
    oms_user_id      INTEGER,
    linked_at        TEXT NOT NULL DEFAULT '',
    last_seen        TEXT NOT NULL DEFAULT '',
    blocked          INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS link_codes (
    code        TEXT PRIMARY KEY,
    oms_user_id INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT NOT NULL DEFAULT '',
    used_by     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cursors (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS position_state (
    trade_leg_id  TEXT PRIMARY KEY,
    open_quantity INTEGER NOT NULL,
    status        TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_state (
    name    TEXT PRIMARY KEY,
    last_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    broker     TEXT NOT NULL,
    at         TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_webhook_log_broker ON webhook_log(broker, id);
CREATE TABLE IF NOT EXISTS secrets (
    name       TEXT PRIMARY KEY,
    value_enc  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
-- ===== Redis-only data model (TradePulse's own copy; the OMS is never queried) =====
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('ADMIN','MANAGER','CLIENT')),
    status      TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','DISABLED')),
    manager_id  INTEGER,
    auth_hash   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'TRADEPULSE',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- Broker accounts, discovered from smx:roster / smx:oms:apikeys. user_id = owning TradePulse user (client).
CREATE TABLE IF NOT EXISTS accounts (
    account_id      TEXT PRIMARY KEY,
    broker          TEXT NOT NULL DEFAULT '',
    client_name     TEXT NOT NULL DEFAULT '',
    owner_username  TEXT NOT NULL DEFAULT '',
    user_id         INTEGER,
    trading_mode    TEXT NOT NULL DEFAULT '',
    in_roster       INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    session_status  TEXT NOT NULL DEFAULT 'UNKNOWN',
    session_updated TEXT NOT NULL DEFAULT '',
    first_seen      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    internal_order_id TEXT PRIMARY KEY,
    execution_id      TEXT NOT NULL DEFAULT '',
    account_id        TEXT NOT NULL,
    broker            TEXT NOT NULL DEFAULT '',
    trading_mode      TEXT NOT NULL DEFAULT '',
    strategy_id       TEXT NOT NULL DEFAULT '',
    exchange          TEXT NOT NULL DEFAULT '',
    exchange_token    INTEGER NOT NULL DEFAULT 0,
    symbol            TEXT NOT NULL DEFAULT '',
    broker_symbol     TEXT NOT NULL DEFAULT '',
    instrument_class  TEXT NOT NULL DEFAULT '',
    side              TEXT NOT NULL DEFAULT '',
    position_effect   TEXT NOT NULL DEFAULT '',
    product           TEXT NOT NULL DEFAULT '',
    order_type        TEXT NOT NULL DEFAULT '',
    ordered_quantity  INTEGER NOT NULL DEFAULT 0,
    price             REAL,
    status            TEXT NOT NULL DEFAULT 'ROUTED',
    status_message    TEXT NOT NULL DEFAULT '',
    broker_order_id   TEXT NOT NULL DEFAULT '',
    filled_quantity   INTEGER NOT NULL DEFAULT 0,
    average_price     REAL NOT NULL DEFAULT 0,
    modification_count INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_broker_id ON orders(broker_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_exec ON orders(execution_id);
CREATE TABLE IF NOT EXISTS order_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    internal_order_id TEXT NOT NULL,
    from_status       TEXT NOT NULL DEFAULT '',
    to_status         TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT '',
    filled_quantity   INTEGER NOT NULL DEFAULT 0,
    average_price     REAL NOT NULL DEFAULT 0,
    message           TEXT NOT NULL DEFAULT '',
    stream_id         TEXT NOT NULL DEFAULT '',
    at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(internal_order_id, id);
CREATE TABLE IF NOT EXISTS fills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id           TEXT NOT NULL,
    account_id        TEXT NOT NULL,
    internal_order_id TEXT NOT NULL DEFAULT '',
    broker_order_id   TEXT NOT NULL DEFAULT '',
    exchange_trade_id TEXT NOT NULL DEFAULT '',
    exchange          TEXT NOT NULL DEFAULT '',
    exchange_token    INTEGER NOT NULL DEFAULT 0,
    symbol            TEXT NOT NULL DEFAULT '',
    product           TEXT NOT NULL DEFAULT '',
    side              TEXT NOT NULL DEFAULT '',
    quantity          INTEGER NOT NULL,
    price             REAL NOT NULL,
    filled_at         TEXT NOT NULL,
    applied_to_book   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    UNIQUE (account_id, fill_id)
);
CREATE INDEX IF NOT EXISTS idx_fills_account ON fills(account_id, filled_at);
CREATE TABLE IF NOT EXISTS workers (
    account_id        TEXT PRIMARY KEY,
    worker_id         TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT '',
    broker            TEXT NOT NULL DEFAULT '',
    broker_connected  INTEGER NOT NULL DEFAULT 0,
    open_orders       INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT NOT NULL DEFAULT '',
    last_heartbeat_at TEXT NOT NULL DEFAULT ''
);
-- The book: broker baseline + fills applied after it. Key = how brokers and the OMS key a position.
CREATE TABLE IF NOT EXISTS book_positions (
    account_id      TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    exchange_token  INTEGER NOT NULL,
    product         TEXT NOT NULL,
    symbol          TEXT NOT NULL DEFAULT '',
    instrument_class TEXT NOT NULL DEFAULT '',
    net_quantity    INTEGER NOT NULL DEFAULT 0,
    average_price   TEXT NOT NULL DEFAULT '0',      -- Decimal as text
    realized        TEXT NOT NULL DEFAULT '0',      -- Decimal as text, since the baseline
    day_buy_qty     INTEGER NOT NULL DEFAULT 0,
    day_sell_qty    INTEGER NOT NULL DEFAULT 0,
    last_price      REAL NOT NULL DEFAULT 0,
    broker_pnl      REAL NOT NULL DEFAULT 0,
    baseline_qty    INTEGER NOT NULL DEFAULT 0,
    baseline_at     TEXT NOT NULL DEFAULT '',
    baseline_source TEXT NOT NULL DEFAULT '',
    fills_applied   INTEGER NOT NULL DEFAULT 0,
    cost_known      INTEGER NOT NULL DEFAULT 1,     -- 0: the broker reported no average price for it
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (account_id, exchange, exchange_token, product)
);
CREATE TABLE IF NOT EXISTS book_holdings (
    account_id      TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    exchange_token  INTEGER NOT NULL,
    product         TEXT NOT NULL DEFAULT 'CNC',
    symbol          TEXT NOT NULL DEFAULT '',
    isin            TEXT NOT NULL DEFAULT '',
    quantity        INTEGER NOT NULL DEFAULT 0,     -- expected: baseline + delivery fills since
    sellable        INTEGER NOT NULL DEFAULT 0,
    t1_quantity     INTEGER NOT NULL DEFAULT 0,
    pledged_quantity INTEGER NOT NULL DEFAULT 0,
    average_price   TEXT NOT NULL DEFAULT '0',
    last_price      REAL NOT NULL DEFAULT 0,
    baseline_qty    INTEGER NOT NULL DEFAULT 0,
    baseline_at     TEXT NOT NULL DEFAULT '',
    baseline_source TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (account_id, exchange, exchange_token, product)
);
-- Account-level baseline marker: when the last positions/holdings snapshot was taken.
CREATE TABLE IF NOT EXISTS book_baselines (
    account_id TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('positions','holdings')),
    at         TEXT NOT NULL,
    source     TEXT NOT NULL,
    rows       INTEGER NOT NULL DEFAULT 0,
    snapshot_id INTEGER,
    PRIMARY KEY (account_id, kind)
);
CREATE TABLE IF NOT EXISTS reconcile_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger     TEXT NOT NULL,          -- SCHEDULED 09:00 | SCHEDULED 09:05 | USER:<name> | OMS_EVENT
    status      TEXT NOT NULL DEFAULT 'RUNNING',
    accounts    INTEGER NOT NULL DEFAULT 0,
    requests    INTEGER NOT NULL DEFAULT 0,
    snapshots   INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    mismatches  INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reconcile_diffs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    account_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    exchange_token INTEGER NOT NULL,
    product     TEXT NOT NULL,
    symbol      TEXT NOT NULL DEFAULT '',
    book_qty    INTEGER NOT NULL,
    broker_qty  INTEGER NOT NULL,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reconcile_diffs_run ON reconcile_diffs(run_id);
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    source      TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    rows        INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '[]',
    run_id      INTEGER,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_account ON snapshots(account_id, kind, id);
CREATE TABLE IF NOT EXISTS pending_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id  TEXT NOT NULL UNIQUE,
    msg_type   TEXT NOT NULL,
    data       TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assistant_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    channel  TEXT NOT NULL,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL,
    tools    TEXT NOT NULL DEFAULT '[]',
    engine   TEXT NOT NULL DEFAULT ''
);
"""

# Runtime-editable settings and their defaults. Edited from Settings -> Monitoring.
DEFAULT_SETTINGS = {
    "oms_poll_interval_ms": 1000,          # max wait between reads of the OMS Redis streams (XREAD blocks until data)
    "oms_stream_wakeup": True,             # (kept for compatibility; Redis streams are always read with XREAD BLOCK)
    "reconcile_enabled": True,             # positions + holdings snapshot of monitored accounts
    "reconcile_times": ["09:00", "09:05"],  # IST, trading days
    "broker_slot_seconds": 3,              # ONE broker request (positions or holdings) per this many seconds, all accounts
    "track_positions": True,               # emit POSITION_UPDATED from OMS trade legs
    "track_alerts": True,                  # emit ACCOUNT_ALERT from OMS alerts
    "notify_modes": ["LIVE", "PAPER"],
    "order_alerts": "final",               # final = one message when an order ends; every = every step     # trading modes that trigger notifications (DRY_RUN is noisy)
    "market_open": "09:15",
    "market_close": "15:30",
    "trading_days": [0, 1, 2, 3, 4],       # Mon..Fri
    "eod_summary_time": "15:40",
    "retry_max_attempts": 8,
    "retry_base_seconds": 5,
    "event_retention_days": 30,
    "telegram_enabled": True,
    "whatsapp_enabled": False,
    "default_digest_interval_min": 0,
}


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wlock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            # Additive migrations for databases created by an earlier version.
            cols = {r[1] for r in c.execute("PRAGMA table_info(book_positions)")}
            if "cost_known" not in cols:
                c.execute("ALTER TABLE book_positions ADD COLUMN cost_known INTEGER NOT NULL DEFAULT 1")
            if "modes" not in {r[1] for r in c.execute("PRAGMA table_info(groups)")}:
                # Trading modes this group is notified about (JSON list); empty = the global setting.
                c.execute("ALTER TABLE groups ADD COLUMN modes TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA busy_timeout = 15000")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    @contextmanager
    def tx(self):
        """Serialized write transaction."""
        with self._wlock, self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            yield c

    def rows(self, sql, params=()) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, params)]

    def one(self, sql, params=()) -> dict | None:
        r = self.rows(sql, params)
        return r[0] if r else None

    def execute(self, sql, params=()) -> int:
        with self.tx() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount

    # --- settings ------------------------------------------------------------------
    def get_settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        for r in self.rows("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
        return out

    def setting(self, key: str):
        return self.get_settings().get(key, DEFAULT_SETTINGS.get(key))

    def put_settings(self, values: dict) -> None:
        with self.tx() as c:
            for k, v in values.items():
                c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (k, json.dumps(v)))

    # --- cursors ---------------------------------------------------------------------
    def cursor(self, name: str, default: str = "") -> str:
        r = self.one("SELECT value FROM cursors WHERE name = ?", (name,))
        return r["value"] if r else default

    def set_cursor(self, name: str, value, conn=None) -> None:
        sql = "INSERT INTO cursors (name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value"
        if conn is not None:
            conn.execute(sql, (name, str(value)))
        else:
            self.execute(sql, (name, str(value)))

    # --- audit -----------------------------------------------------------------------
    def audit(self, actor: str, action: str, target: str = "", detail="") -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        self.execute("INSERT INTO audit (at, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                     (ist_iso(), actor, action, str(target), detail[:2000]))

    # --- monitoring ------------------------------------------------------------------
    def monitored(self) -> dict[str, dict]:
        return {r["account_id"]: r for r in self.rows("SELECT * FROM monitored_accounts")}

    def monitored_ids(self) -> set[str]:
        return {r["account_id"] for r in self.rows("SELECT account_id FROM monitored_accounts WHERE enabled = 1")}

    def set_monitoring(self, account_ids: list[str], enabled: bool, actor: str) -> int:
        now = ist_iso()
        with self.tx() as c:
            for a in account_ids:
                c.execute("""INSERT INTO monitored_accounts (account_id, enabled, updated_by, updated_at)
                             VALUES (?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET
                             enabled = excluded.enabled, updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
                          (a, int(enabled), actor, now))
        return len(account_ids)

    def set_muted(self, account_id: str, muted: bool, actor: str) -> None:
        self.execute("""INSERT INTO monitored_accounts (account_id, enabled, muted, updated_by, updated_at)
                        VALUES (?, 0, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET muted = excluded.muted,
                        updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
                     (account_id, int(muted), actor, ist_iso()))

    # --- groups / channels -------------------------------------------------------------
    def groups(self) -> list[dict]:
        gs = self.rows("SELECT * FROM groups ORDER BY name")
        acc = self.rows("SELECT group_id, account_id FROM group_accounts")
        ch = self.rows("SELECT gc.group_id, c.* FROM group_channels gc JOIN channels c ON c.id = gc.channel_id")
        for g in gs:
            g["digest_times"] = json.loads(g["digest_times"] or "[]")
            try:
                g["modes"] = json.loads(g.get("modes") or "[]")
            except ValueError:
                g["modes"] = []
            g["account_ids"] = [a["account_id"] for a in acc if a["group_id"] == g["id"]]
            g["channels"] = [{k: c[k] for k in ("id", "kind", "chat_id", "title", "chat_type", "enabled", "status")}
                             for c in ch if c["group_id"] == g["id"]]
            g["rules"] = self.rules_for("group", str(g["id"]))
        return gs

    def group(self, group_id: int) -> dict | None:
        return next((g for g in self.groups() if g["id"] == group_id), None)

    def channels(self) -> list[dict]:
        chans = self.rows("SELECT * FROM channels ORDER BY kind, title")
        links = self.rows("SELECT gc.channel_id, g.id, g.name FROM group_channels gc JOIN groups g ON g.id = gc.group_id")
        for c in chans:
            c["groups"] = [{"id": l["id"], "name": l["name"]} for l in links if l["channel_id"] == c["id"]]
        return chans

    def channel_by_chat(self, kind: str, chat_id: str) -> dict | None:
        return self.one("SELECT * FROM channels WHERE kind = ? AND chat_id = ?", (kind, str(chat_id)))

    def upsert_channel(self, kind: str, chat_id: str, title: str = "", chat_type: str = "",
                       enabled: bool | None = None, status: str | None = None) -> dict:
        now = ist_iso()
        with self.tx() as c:
            c.execute("""INSERT INTO channels (kind, chat_id, title, chat_type, enabled, status, last_seen, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                         ON CONFLICT(kind, chat_id) DO UPDATE SET
                           title = CASE WHEN excluded.title != '' THEN excluded.title ELSE channels.title END,
                           chat_type = CASE WHEN excluded.chat_type != '' THEN excluded.chat_type ELSE channels.chat_type END,
                           last_seen = excluded.last_seen""",
                      (kind, str(chat_id), title, chat_type, int(bool(enabled)), status or "PENDING", now, now))
            if enabled is not None:
                c.execute("UPDATE channels SET enabled = ? WHERE kind = ? AND chat_id = ?", (int(enabled), kind, str(chat_id)))
            if status is not None:
                c.execute("UPDATE channels SET status = ? WHERE kind = ? AND chat_id = ?", (status, kind, str(chat_id)))
        return self.channel_by_chat(kind, chat_id)

    def migrate_channel(self, kind: str, old_chat_id: str, new_chat_id: str) -> None:
        """Telegram upgraded a group to a supergroup (new chat id). Keep ONE row: if the new id is already
        known (the bot saw it first), move the old row's settings and group links onto it and drop the old row."""
        with self.tx() as c:
            old = c.execute("SELECT * FROM channels WHERE kind = ? AND chat_id = ?", (kind, old_chat_id)).fetchone()
            if not old:
                return
            new = c.execute("SELECT * FROM channels WHERE kind = ? AND chat_id = ?", (kind, new_chat_id)).fetchone()
            if not new:
                c.execute("UPDATE channels SET chat_id = ?, chat_type = 'supergroup' WHERE id = ?", (new_chat_id, old["id"]))
                return
            if old["status"] == "ACTIVE" and new["status"] != "ACTIVE":
                c.execute("UPDATE channels SET status = 'ACTIVE', enabled = ? WHERE id = ?", (old["enabled"], new["id"]))
            c.execute("INSERT OR IGNORE INTO group_channels (group_id, channel_id) SELECT group_id, ? FROM group_channels "
                      "WHERE channel_id = ?", (new["id"], old["id"]))
            c.execute("UPDATE notifications SET channel_id = ? WHERE channel_id = ? AND status IN ('QUEUED','FAILED')",
                      (new["id"], old["id"]))
            c.execute("DELETE FROM channels WHERE id = ?", (old["id"],))

    def delete_channel(self, channel_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM notifications WHERE channel_id = ? AND status IN ('QUEUED','FAILED','DEAD')", (channel_id,))
            c.execute("DELETE FROM group_channels WHERE channel_id = ?", (channel_id,))
            c.execute("DELETE FROM channels WHERE id = ?", (channel_id,))

    def groups_for_account(self, account_id: str) -> list[dict]:
        return [g for g in self.groups() if account_id in g["account_ids"]]

    def groups_for_channel(self, channel_id: int) -> list[dict]:
        return [g for g in self.groups() if any(c["id"] == channel_id for c in g["channels"])]

    # --- rules -------------------------------------------------------------------------
    def rules_for(self, scope: str, scope_id: str = "") -> dict:
        return {r["event_type"]: bool(r["enabled"]) for r in
                self.rows("SELECT event_type, enabled FROM rules WHERE scope = ? AND scope_id = ?", (scope, scope_id))}

    def all_rules(self) -> list[dict]:
        return self.rows("SELECT * FROM rules ORDER BY scope, scope_id, event_type")

    def set_rules(self, scope: str, scope_id: str, values: dict) -> None:
        """values: event_type -> True/False/None (None removes the override = inherit)."""
        with self.tx() as c:
            for et, v in values.items():
                if v is None:
                    c.execute("DELETE FROM rules WHERE scope = ? AND scope_id = ? AND event_type = ?", (scope, scope_id, et))
                else:
                    c.execute("""INSERT INTO rules (scope, scope_id, event_type, enabled) VALUES (?, ?, ?, ?)
                                 ON CONFLICT(scope, scope_id, event_type) DO UPDATE SET enabled = excluded.enabled""",
                              (scope, scope_id, et, int(bool(v))))
