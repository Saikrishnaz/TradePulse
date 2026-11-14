"""Dashboard authentication with TradePulse's OWN users (the OMS is unreachable after hours).

* Users live in TradePulse's ``users`` table: imported once from the OMS (same usernames, bcrypt
  hashes and roles - ``python -m tradepulse.admin import-oms``), then managed on the Users page.
  A bootstrap admin is created from ``TP_ADMIN_USERNAME``/``TP_ADMIN_PASSWORD`` when there are none.
* Sign-in checks bcrypt, then issues a TradePulse session (own table, HttpOnly cookie ``tp_session``).
* Every request re-reads the user, so disabling a user locks them out on their next request.
* Roles: ADMIN may change configuration and users; MANAGER and CLIENT get read access to their own
  scope (same visibility rules as the OMS).
* CSRF: mutating requests must carry ``X-TradePulse: 1`` - a header a cross-site form cannot set.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

import bcrypt
from fastapi import HTTPException, Request, Response

from .config import IST, ist_iso, now_ist


class RateLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_s: int = 60) -> bool:
        """True when allowed."""
        q = self._hits[key]
        now = time.monotonic()
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(str(plain).encode(), hashed.encode())
    except ValueError:
        return False


class Auth:
    def __init__(self, store, oms, settings):
        self.store, self.oms, self.settings = store, oms, settings
        self.limiter = RateLimiter()          # per app instance

    def login(self, username: str, password: str, request: Request, response: Response) -> dict:
        ip = request.client.host if request.client else "?"
        if not self.limiter.hit(f"login:{ip}", 10) or not self.limiter.hit(f"login-user:{username.lower()}", 10):
            raise HTTPException(429, "Too many sign-in attempts. Wait a minute and try again.")
        user = self.oms.user_by_username(username)
        if not user or user["status"] != "ACTIVE" or not verify_password(password, user.get("auth_hash") or ""):
            self.store.audit(username[:64] or "?", "LOGIN_FAILED", ip)
            raise HTTPException(401, "Wrong username or password.")
        token = secrets.token_urlsafe(40)
        exp = (now_ist() + timedelta(hours=self.settings.SESSION_HOURS)).astimezone(IST).isoformat(timespec="seconds")
        self.store.execute("INSERT INTO sessions (token, oms_user_id, created_at, expires_at, ip, user_agent) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (token, user["id"], ist_iso(), exp, ip, request.headers.get("user-agent", "")[:300]))
        response.set_cookie(self.settings.COOKIE_NAME, token, httponly=True, samesite="lax",
                            secure=self.settings.COOKIE_SECURE, max_age=self.settings.SESSION_HOURS * 3600, path="/")
        self.store.audit(user["username"], "LOGIN", ip)
        return public_user(user)

    def logout(self, request: Request, response: Response) -> None:
        token = request.cookies.get(self.settings.COOKIE_NAME)
        if token:
            self.store.execute("DELETE FROM sessions WHERE token = ?", (token,))
        response.delete_cookie(self.settings.COOKIE_NAME, path="/")

    def current(self, request: Request) -> dict:
        token = request.cookies.get(self.settings.COOKIE_NAME)
        now = ist_iso()
        if token:
            s = self.store.one("SELECT * FROM sessions WHERE token = ?", (token,))
            if s and s["expires_at"] >= now:
                u = self.oms.user_by_id(int(s["oms_user_id"]))
                if u and u["status"] == "ACTIVE":
                    return u
            if s:
                self.store.execute("DELETE FROM sessions WHERE token = ?", (token,))
        raise HTTPException(401, "Not signed in")

    def purge(self) -> None:
        self.store.execute("DELETE FROM sessions WHERE expires_at < ?", (ist_iso(),))


def public_user(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "name": u["name"], "role": u["role"],
            "can_configure": u["role"] == "ADMIN"}
