"""Secrets TradePulse stores itself (Telegram bot token, webhook secrets), Fernet-encrypted at rest.

The key comes from ``TP_FERNET_KEY``. If it is unset (development), a key is generated once into
``data/.tp_fernet.key`` and the health page warns about it. Plaintext secrets are never returned
by the API - only whether one is set and its last 4 characters.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import ist_iso

log = logging.getLogger("tradepulse.vault")


class Vault:
    def __init__(self, store, key: str = "", key_file: Path | None = None):
        self.store = store
        self.generated_key = False
        if not key:
            key_file = key_file or (store.path.parent / ".tp_fernet.key")
            if key_file.exists():
                key = key_file.read_text().strip()
            else:
                key = Fernet.generate_key().decode()
                key_file.write_text(key)
                try:
                    os.chmod(key_file, 0o600)
                except OSError:
                    pass
            self.generated_key = True
            log.warning("TP_FERNET_KEY not set; using a generated key in %s (set it in production)", key_file)
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def put(self, name: str, value: str) -> None:
        if not value:
            self.store.execute("DELETE FROM secrets WHERE name = ?", (name,))
            return
        self.store.execute("""INSERT INTO secrets (name, value_enc, updated_at) VALUES (?, ?, ?)
                              ON CONFLICT(name) DO UPDATE SET value_enc = excluded.value_enc, updated_at = excluded.updated_at""",
                           (name, self._f.encrypt(value.encode()).decode(), ist_iso()))

    def get(self, name: str) -> str:
        r = self.store.one("SELECT value_enc FROM secrets WHERE name = ?", (name,))
        if not r:
            return ""
        try:
            return self._f.decrypt(r["value_enc"].encode()).decode()
        except InvalidToken:
            log.error("secret %s cannot be decrypted (TP_FERNET_KEY changed?)", name)
            return ""

    def describe(self, name: str) -> dict:
        v = self.get(name)
        r = self.store.one("SELECT updated_at FROM secrets WHERE name = ?", (name,))
        return {"set": bool(v), "hint": ("…" + v[-4:]) if len(v) >= 8 else ("set" if v else ""),
                "updated_at": (r or {}).get("updated_at", "")}

    def names(self, prefix: str = "") -> list[str]:
        return [r["name"] for r in self.store.rows("SELECT name FROM secrets WHERE name LIKE ? ORDER BY name", (prefix + "%",))]
