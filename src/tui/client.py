"""Async HTTP client for the backend API. The only thing the TUI talks to."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

import httpx


class BackendError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Unauthorized(BackendError):
    pass


class Disconnected(BackendError):
    pass


def check_backend_url(url: str) -> str | None:
    """Why `url` must not be used (None if fine). Credentials and the session token
    travel in plaintext over http, so http is allowed only to loopback hosts."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return f"invalid backend URL: {url!r}"
    if parts.scheme not in ("http", "https") or not host:
        return "backend URL must start with https:// (or http:// for localhost)"
    if parts.scheme == "https":
        return None
    if host == "localhost":
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return f"refusing plaintext http:// to {host}: use https:// (http is only allowed for localhost)"


class BackendClient:
    def __init__(self, base_url: str, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport,
                                       follow_redirects=False)  # never resend the token elsewhere
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
        if r.is_redirect:  # not followed: the bearer token must not be replayed to another URL
            raise BackendError(f"backend redirected to {r.headers.get('location', '?')}; "
                               "refusing to follow (use the final URL directly)", r.status_code)
        if not r.is_success:  # e.g. a 304 or other non-2xx that slipped past the checks above
            raise BackendError(f"unexpected response from backend ({r.status_code})", r.status_code)
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as exc:
            raise BackendError(f"malformed response from backend ({r.status_code})", r.status_code) from exc

    async def health(self) -> dict:
        return await self._req("GET", "/health")

    async def login(self, username: str, password: str) -> dict:
        out = await self._req("POST", "/auth/login", json={"username": username, "password": password})
        if not (isinstance(out, dict) and isinstance(out.get("token"), str) and isinstance(out.get("username"), str)):
            raise BackendError("malformed login response from backend", 200)
        self._token, self.username = out["token"], out["username"]
        return out

    async def logout(self) -> None:
        try:
            if self._token:
                await self._req("POST", "/auth/logout")
        finally:
            self._token = self.username = None

    async def dashboard(self, events_after: int = 0) -> dict:
        data = await self._req("GET", "/dashboard", params={"events_after": events_after})
        if not (isinstance(data, dict)
                and all(isinstance(data.get(k), list) for k in ("approvals", "working", "history", "events"))):
            raise BackendError("malformed dashboard response from backend", 200)
        return data

    async def approve(self, action_id: int, payload_digest: str | None = None) -> dict:
        """`payload_digest` binds the approval to the draft the reviewer saw (409 if it changed)."""
        body = {"payload_digest": payload_digest} if payload_digest else None
        return await self._req("POST", f"/actions/{action_id}/approve", json=body)

    async def reject(self, action_id: int) -> dict:
        return await self._req("POST", f"/actions/{action_id}/reject")

    async def edit(self, action_id: int, changes: dict[str, str]) -> dict:
        return await self._req("PATCH", f"/actions/{action_id}", json={"changes": changes})

    async def retry(self, email_id: str) -> dict:
        return await self._req("POST", f"/emails/{email_id}/retry")

    async def reopen(self, action_id: int) -> dict:
        """Failed execution that never reached the API -> back to pending for review."""
        return await self._req("POST", f"/actions/{action_id}/reopen")


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text or r.reason_phrase
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return r.text or r.reason_phrase
