"""Клиент GigaChat API.

Авторизация: POST {auth_url}/api/v2/oauth (Basic <ключ авторизации>, scope GIGACHAT_API_PERS) → access_token
на 30 минут; затем POST {api_url}/api/v1/chat/completions. В ЛР4 оба адреса указывают на мок
(mocks/gigachat) с тем же контрактом.
"""
from __future__ import annotations

import time
import uuid

import httpx


class LlmUnavailable(Exception):
    pass


class GigaChat:
    def __init__(self, api_url: str, auth_url: str, auth_key: str, scope: str = "GIGACHAT_API_PERS",
                 model: str = "GigaChat", timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None,
                 verify: bool | str = True):
        self.api = httpx.AsyncClient(base_url=api_url, timeout=timeout, transport=transport, verify=verify)
        self.auth = httpx.AsyncClient(base_url=auth_url, timeout=10, transport=transport, verify=verify)
        self.auth_key, self.scope, self.model = auth_key, scope, model
        self._token, self._token_exp = None, 0.0

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        try:
            r = await self.auth.post("/api/v2/oauth", data={"scope": self.scope},
                                     headers={"Authorization": f"Basic {self.auth_key}", "RqUID": str(uuid.uuid4())})
        except httpx.TransportError as exc:
            raise LlmUnavailable(f"OAuth: {exc}") from exc
        if r.status_code != 200:
            raise LlmUnavailable(f"OAuth HTTP {r.status_code}")
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = data.get("expires_at", (time.time() + 1800) * 1000) / 1000
        return self._token

    async def chat(self, messages: list[dict]) -> str:
        token = await self._access_token()
        try:
            r = await self.api.post("/api/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                                    json={"model": self.model, "messages": messages, "temperature": 0.1})
        except httpx.TransportError as exc:
            raise LlmUnavailable(str(exc)) from exc
        if r.status_code == 401:
            self._token = None
            raise LlmUnavailable("токен GigaChat истёк")
        if r.status_code >= 400:
            raise LlmUnavailable(f"GigaChat HTTP {r.status_code}")
        return r.json()["choices"][0]["message"]["content"]
