"""Minimal Telegram Bot API client (httpx). The token is never logged."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("tradepulse.telegram")


class TelegramError(Exception):
    def __init__(self, code: int, description: str, retry_after: int | None = None):
        super().__init__(f"{code}: {description}")
        self.code, self.description, self.retry_after = code, description, retry_after

    @property
    def permanent(self) -> bool:
        """Errors resending can never fix: bad chat, bot removed, bad token."""
        d = self.description.lower()
        return self.code in (401, 403, 404) or (self.code == 400 and (
            "chat not found" in d or "bot was kicked" in d or "not enough rights" in d or "user is deactivated" in d
            or "chat_write_forbidden" in d or "have no rights" in d))


class TelegramClient:
    def __init__(self, token: str, api: str = "https://api.telegram.org", http: httpx.Client | None = None):
        if not token:
            raise ValueError("Telegram bot token is not configured")
        self._token = token
        self._base = f"{api.rstrip('/')}/bot{token}"
        self._http = http or httpx.Client(timeout=httpx.Timeout(35.0, connect=5.0))

    def call(self, method: str, _http_timeout: float | None = None, **params):
        try:
            kw = {"timeout": _http_timeout} if _http_timeout else {}
            r = self._http.post(f"{self._base}/{method}", json={k: v for k, v in params.items() if v is not None}, **kw)
        except httpx.HTTPError as ex:
            raise TelegramError(0, f"network: {type(ex).__name__}") from None
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(r.status_code, "non-JSON response") from None
        if not data.get("ok"):
            params_ = data.get("parameters") or {}
            raise TelegramError(int(data.get("error_code") or r.status_code), str(data.get("description") or ""),
                                params_.get("retry_after"))
        return data.get("result")

    def get_me(self) -> dict:
        return self.call("getMe")

    def send_message(self, chat_id, text: str, reply_to: int | None = None) -> dict:
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         disable_web_page_preview=True,
                         reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None)

    def send_chat_action(self, chat_id, action: str = "typing") -> None:
        try:
            self.call("sendChatAction", chat_id=chat_id, action=action)
        except TelegramError:
            pass

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        return self.call("getUpdates", _http_timeout=timeout + 10, offset=offset, timeout=timeout,
                         allowed_updates=["message", "my_chat_member"]) or []

    def set_webhook(self, url: str, secret_token: str) -> bool:
        return bool(self.call("setWebhook", url=url, secret_token=secret_token,
                              allowed_updates=["message", "my_chat_member"]))

    def delete_webhook(self) -> bool:
        return bool(self.call("deleteWebhook"))

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])
