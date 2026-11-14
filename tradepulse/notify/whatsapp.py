"""WhatsApp Business Cloud API provider (Phase 7 - optional).

Independent of Telegram: when it is not configured every WhatsApp notification is marked SKIPPED
and Telegram is unaffected. Business-initiated messages outside a 24h customer window require an
approved template; set ``whatsapp_template`` to send through a template whose single body
parameter is the notification text.
"""

from __future__ import annotations

import re

import httpx

GRAPH = "https://graph.facebook.com/v21.0"


class WhatsAppError(Exception):
    def __init__(self, code: int, description: str, permanent: bool = False):
        super().__init__(f"{code}: {description}")
        self.code, self.description, self.permanent = code, description, permanent
        self.retry_after = None


class WhatsAppProvider:
    def __init__(self, vault, http: httpx.Client | None = None):
        self.vault = vault
        self._client = http

    @property
    def _http(self) -> httpx.Client:
        if self._client is None:                       # built on first use: most installs never send WhatsApp
            self._client = httpx.Client(timeout=15)
        return self._client

    def configured(self) -> bool:
        return bool(self.vault.get("whatsapp_token") and self.vault.get("whatsapp_phone_number_id"))

    def send(self, to: str, html_text: str, template: str = "") -> str:
        token, phone_id = self.vault.get("whatsapp_token"), self.vault.get("whatsapp_phone_number_id")
        if not token or not phone_id:
            raise WhatsAppError(0, "WhatsApp is not configured", permanent=True)
        text = re.sub(r"<[^>]+>", "", html_text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        if template:
            body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                    "template": {"name": template, "language": {"code": "en"},
                                 "components": [{"type": "body", "parameters": [{"type": "text", "text": text[:1000]}]}]}}
        else:
            body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096]}}
        try:
            r = self._http.post(f"{GRAPH}/{phone_id}/messages", json=body, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as ex:
            raise WhatsAppError(0, f"network: {type(ex).__name__}") from None
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            err = data.get("error") or {}
            raise WhatsAppError(r.status_code, str(err.get("message") or r.text[:200]),
                                permanent=r.status_code in (400, 401, 403, 404))
        return str(((data.get("messages") or [{}])[0]).get("id") or "")
