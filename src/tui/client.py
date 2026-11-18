"""Async HTTP client for the backend API. The only thing the TUI talks to."""

from __future__ import annotations

from typing import Any

import httpx


class BackendError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Unauthorized(BackendError):
    pass


class Disconnected(BackendError):
    pass


class BackendClient:
    def __init__(self, base_url: str, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)
        self._token: str | None = None
        self.username: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kw) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            r = await self._http.request(method, path, headers=headers, **kw)
        except httpx.TransportError as exc:
            raise Disconnected(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if r.status_code == 401:
            raise Unauthorized(_detail(r), 401)
        if r.status_code >= 400:
            raise BackendError(_detail(r), r.status_code)
        return r.json() if r.content else None

    async def health(self) -> dict:
        return await self._req("GET", "/health")

    async def login(self, username: str, password: str) -> dict:
        out = await self._req("POST", "/auth/login", json={"username": username, "password": password})
        self._token, self.username = out["token"], out["username"]
        return out

    async def logout(self) -> None:
        try:
            if self._token:
                await self._req("POST", "/auth/logout")
        finally:
            self._token = self.username = None

    async def dashboard(self, events_after: int = 0) -> dict:
        return await self._req("GET", "/dashboard", params={"events_after": events_after})

    async def approve(self, action_id: int) -> dict:
        return await self._req("POST", f"/actions/{action_id}/approve")

    async def reject(self, action_id: int) -> dict:
        return await self._req("POST", f"/actions/{action_id}/reject")

    async def edit(self, action_id: int, changes: dict[str, str]) -> dict:
        return await self._req("PATCH", f"/actions/{action_id}", json={"changes": changes})

    async def retry(self, email_id: str) -> dict:
        return await self._req("POST", f"/emails/{email_id}/retry")


def _detail(r: httpx.Response) -> str:
    try:
        return str(r.json().get("detail", r.text))
    except ValueError:
        return r.text or r.reason_phrase
