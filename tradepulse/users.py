"""TradePulse user directory: bootstrap admin, CRUD for the Users page, one-time import from the OMS.

    python -m tradepulse.admin import-oms --db /opt/skymatrix/data/skymatrix.db
    python -m tradepulse.admin create-admin --username admin

The import is the only time TradePulse ever opens the OMS database, and it opens it read-only. It copies
usernames, names, roles, manager links, bcrypt hashes (so OMS passwords keep working) and which user
owns which broker account. Re-running it adds new users/accounts and never overwrites an existing
TradePulse user (passwords changed in TradePulse stay changed).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import bcrypt

from .config import ist_iso

log = logging.getLogger("tradepulse.users")
ROLES = ("ADMIN", "MANAGER", "CLIENT")


def hash_password(plain: str) -> str:
    import os
    rounds = max(4, min(15, int(os.getenv("TP_BCRYPT_ROUNDS", "12") or 12)))      # tests lower it; production keeps 12
    return bcrypt.hashpw(str(plain).encode(), bcrypt.gensalt(rounds=rounds)).decode()


def ensure_bootstrap_admin(store, username: str, password: str) -> bool:
    if store.one("SELECT 1 AS x FROM users LIMIT 1"):
        return False
    if not password:
        log.warning("No TradePulse users yet. Set TP_ADMIN_PASSWORD (or run `python -m tradepulse.admin import-oms`) "
                    "to create the first administrator.")
        return False
    now = ist_iso()
    store.execute("INSERT INTO users (username, name, role, auth_hash, source, created_at, updated_at) "
                  "VALUES (?, 'Administrator', 'ADMIN', ?, 'BOOTSTRAP', ?, ?)", (username, hash_password(password), now, now))
    log.info("bootstrap administrator %s created", username)
    return True


def create_user(store, username: str, name: str, role: str, password: str, manager_id: int | None = None) -> int:
    if role not in ROLES:
        raise ValueError("role must be ADMIN, MANAGER or CLIENT")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    now = ist_iso()
    return store.execute("INSERT INTO users (username, name, role, auth_hash, manager_id, source, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, 'TRADEPULSE', ?, ?)",
                         (username.strip(), name.strip(), role, hash_password(password), manager_id, now, now))


def import_oms(store, oms_db: str | Path) -> dict:
    """Copy users + account ownership from an OMS SQLite file (read-only). Idempotent."""
    path = Path(oms_db)
    if not path.exists():
        raise FileNotFoundError(path)
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        src.execute("PRAGMA query_only = ON")
        users = [dict(r) for r in src.execute("SELECT id, username, name, role, status, manager_id, auth_hash FROM users")]
        accts = [dict(r) for r in src.execute("SELECT b.account_id, LOWER(b.broker) AS broker, b.user_id, u.username, u.name "
                                              "FROM broker_accounts b JOIN users u ON u.id = b.user_id")]
    finally:
        src.close()
    now = ist_iso()
    id_map: dict[int, int] = {}
    added = 0
    with store.tx() as c:
        for u in users:
            if u["role"] not in ROLES:
                continue
            existing = c.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (u["username"],)).fetchone()
            if existing:
                id_map[u["id"]] = existing[0]
                continue
            id_map[u["id"]] = c.execute(
                "INSERT INTO users (username, name, role, status, auth_hash, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'OMS_IMPORT', ?, ?)",
                (u["username"], u["name"], u["role"], "ACTIVE" if u["status"] == "ACTIVE" else "DISABLED", u["auth_hash"] or "", now, now)).lastrowid
            added += 1
        for u in users:
            if u.get("manager_id") and u["id"] in id_map and u["manager_id"] in id_map:
                c.execute("UPDATE users SET manager_id = ? WHERE id = ? AND manager_id IS NULL", (id_map[u["manager_id"]], id_map[u["id"]]))
        for a in accts:
            c.execute("""INSERT INTO accounts (account_id, broker, client_name, owner_username, user_id, first_seen, updated_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET
                           user_id = COALESCE(accounts.user_id, excluded.user_id),
                           broker = CASE WHEN accounts.broker = '' THEN excluded.broker ELSE accounts.broker END,
                           client_name = CASE WHEN accounts.client_name = '' THEN excluded.client_name ELSE accounts.client_name END,
                           owner_username = CASE WHEN accounts.owner_username = '' THEN excluded.owner_username ELSE accounts.owner_username END""",
                      (a["account_id"], a["broker"], a["name"], a["username"], id_map.get(a["user_id"]), now, now))
    store.audit("system", "OMS_IMPORT", str(path), {"users_added": added, "accounts": len(accts)})
    return {"users_added": added, "users_total": len(users), "accounts": len(accts)}
