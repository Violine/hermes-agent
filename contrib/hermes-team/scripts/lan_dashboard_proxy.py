#!/usr/bin/env python3
"""LAN-only authenticated HTTP/WebSocket proxy for the Hermes dashboard.

This proxy keeps Hermes' dashboard authentication enabled. It logs in to the
fixed gateway container using a mode-0600 credential file, keeps the returned
session cookies in process memory, and injects them upstream. Browser cookies
are never forwarded and upstream Set-Cookie headers are never returned.

Configuration is intentionally small:
  HERMES_DASHBOARD_CREDENTIALS=/home/alex/apps/hermes-vpn/.dashboard-proxy-credentials.json
  HERMES_DASHBOARD_BIND=192.168.1.20
  HERMES_DASHBOARD_LAN_CIDR=192.168.1.0/24

The upstream container name, port, and protocol are constants below. There is
no environment variable or request header that can select an arbitrary host.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    ClientWSTimeout,
    DummyCookieJar,
    WSMsgType,
    WSServerHandshakeError,
    web,
)
from multidict import CIMultiDict


UPSTREAM_CONTAINER = "hermes-vpn-gateway"
UPSTREAM_PORT = 9119
PUBLIC_PORT = 9119
PROVIDER = "basic"
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_WS_MESSAGE_BYTES = 8 * 1024 * 1024
AUTH_COOKIE_BASES = frozenset(
    {"hermes_session_at", "hermes_session_rt", "hermes_session_provider"}
)
FORWARDED_HEADERS = frozenset(
    {
        "forwarded", "x-forwarded-for", "x-forwarded-host",
        "x-forwarded-proto", "x-forwarded-port", "x-forwarded-prefix",
        "x-real-ip", "x-client-ip", "client-ip", "true-client-ip",
        "cf-connecting-ip", "x-original-forwarded-for",
    }
)
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailer", "transfer-encoding",
        "upgrade",
    }
)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ALLOWED_AUTH_PATHS = frozenset(
    {"/api/auth/providers", "/api/auth/me", "/api/auth/ws-ticket"}
)
MUTATING_AUTH_PATHS = frozenset(
    {"/auth/logout", "/auth/password-login", "/auth/native/token",
     "/auth/native/refresh"}
)


class ProxyError(RuntimeError):
    """Configuration or upstream authentication failure."""


@dataclass(frozen=True)
class Credentials:
    public_url: str
    username: str
    password: str


def load_credentials(path: str | Path) -> Credentials:
    """Read a strict mode-0600 credential JSON without following symlinks."""
    credential_path = Path(path)
    try:
        info = credential_path.lstat()
    except OSError as exc:
        raise ProxyError(f"cannot stat dashboard credentials: {exc.strerror}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ProxyError("dashboard credential file must be a regular mode-0600 file")
    try:
        value = json.loads(credential_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProxyError("dashboard credential file is unreadable or invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProxyError("dashboard credential JSON must be an object")
    public_url = value.get("url")
    username = value.get("username")
    password = value.get("password")
    if not all(isinstance(x, str) and x for x in (public_url, username, password)):
        raise ProxyError("dashboard credential JSON requires non-empty url, username and password")
    parsed = urlsplit(public_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "192.168.1.20"
        or parsed.port not in (None, PUBLIC_PORT)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ProxyError("credential url must be the fixed local dashboard origin")
    return Credentials(public_url="http://192.168.1.20:9119", username=username, password=password)


def validate_lan(bind_host: str, cidr: str) -> ipaddress.IPv4Network:
    try:
        bind_ip = ipaddress.ip_address(bind_host)
        network = ipaddress.ip_network(cidr, strict=True)
    except ValueError as exc:
        raise ProxyError("invalid dashboard bind address or LAN CIDR") from exc
    if (
        bind_ip != ipaddress.ip_address("192.168.1.20")
        or not isinstance(network, ipaddress.IPv4Network)
        or not network.is_private
        or network.prefixlen < 16
        or network.prefixlen > 30
        or bind_ip not in network
    ):
        raise ProxyError("bind must be 192.168.1.20 and LAN CIDR must be a private /16 through /30 containing it")
    return network


def _host_is_valid(request: web.Request) -> bool:
    values = [v.decode("latin1") for k, v in request.raw_headers if k.lower() == b"host"]
    return values == ["192.168.1.20:9119"]


def _header_values(request: web.Request, name: bytes) -> list[str]:
    return [v.decode("latin1") for k, v in request.raw_headers if k.lower() == name.lower()]


def _origin_is_valid(request: web.Request) -> bool:
    origins = _header_values(request, b"origin")
    return origins == ["http://192.168.1.20:9119"]


def _peer_ip(request: web.Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    transport = request.transport
    peer = transport.get_extra_info("peername") if transport else None
    if not peer:
        return None
    try:
        return ipaddress.ip_address(peer[0])
    except (ValueError, TypeError):
        return None


def _cookie_is_auth(name: str) -> bool:
    normalized = name
    for prefix in ("__Host-", "__Secure-"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    return normalized in AUTH_COOKIE_BASES


def _split_cookies(raw: str) -> dict[str, str]:
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return {}
    return {name: morsel.value for name, morsel in jar.items()}


def _extract_auth_set_cookies(headers: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in headers:
        jar = SimpleCookie()
        try:
            jar.load(value)
        except Exception:
            continue
        for name, morsel in jar.items():
            if _cookie_is_auth(name):
                if morsel["max-age"] == "0" or not morsel.value:
                    result[name] = ""
                else:
                    result[name] = morsel.value
    return result


def _filtered_response_headers(headers) -> CIMultiDict[str]:
    connection_tokens = {
        token.strip().lower()
        for value in headers.getall("Connection", [])
        for token in value.split(",")
        if token.strip()
    }
    excluded = HOP_BY_HOP_HEADERS | connection_tokens | FORWARDED_HEADERS | {"set-cookie"}
    output: CIMultiDict[str] = CIMultiDict()
    for name, value in headers.items():
        if name.lower() not in excluded:
            output.add(name, value)
    return output


async def _limited_body(request: web.Request):
    total = 0
    async for chunk in request.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_BODY_BYTES,
                actual_size=total,
            )
        yield chunk


def _redirect_location_is_local(location: str, public_url: str) -> bool:
    parsed = urlsplit(location)
    if not parsed.scheme and not parsed.netloc:
        return location.startswith("/") and not location.startswith("//")
    public = urlsplit(public_url)
    return (
        parsed.scheme == public.scheme
        and parsed.hostname == public.hostname
        and parsed.port in (None, PUBLIC_PORT)
        and not parsed.username
        and not parsed.password
    )


class DashboardProxy:
    def __init__(
        self,
        credentials: Credentials,
        lan_network: ipaddress.IPv4Network,
        *,
        upstream_base_url: str | None = None,
    ):
        self.credentials = credentials
        self.lan_network = lan_network
        # Injection is available to isolated tests only. Production always
        # discovers the one fixed container and validates its IP literal.
        self._test_upstream = upstream_base_url
        self.upstream_base_url: str | None = None
        self.cookies: dict[str, str] = {}
        self.cookie_generation = 0
        self.auth_lock = asyncio.Lock()
        self.client: ClientSession | None = None

    async def start(self, app: web.Application) -> None:
        self.client = ClientSession(
            cookie_jar=DummyCookieJar(),
            timeout=ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=None),
            auto_decompress=False,
        )
        if self._test_upstream:
            self.upstream_base_url = self._test_upstream.rstrip("/")
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect", "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                UPSTREAM_CONTAINER,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ProxyError("cannot inspect the configured Hermes gateway container") from exc
        address = stdout.decode("ascii", "ignore").strip()
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ProxyError("gateway container did not provide a valid IP address") from exc
        if proc.returncode != 0 or not isinstance(ip, ipaddress.IPv4Address) or not ip.is_private:
            raise ProxyError("gateway container IP is not a private IPv4 address")
        self.upstream_base_url = f"http://{ip}:{UPSTREAM_PORT}"

    async def close(self, app: web.Application) -> None:
        if self.client:
            await self.client.close()

    async def ensure_session(self) -> int:
        if self.cookies:
            return self.cookie_generation
        async with self.auth_lock:
            if self.cookies:
                return self.cookie_generation
            if not self.client or not self.upstream_base_url:
                raise ProxyError("proxy upstream is not initialized")
            async with self.client.post(
                self.upstream_base_url + "/auth/password-login",
                json={
                    "provider": PROVIDER,
                    "username": self.credentials.username,
                    "password": self.credentials.password,
                    "next": "/engineering-team",
                },
                headers={"Host": "192.168.1.20:9119", "Accept": "application/json"},
                allow_redirects=False,
            ) as response:
                body = await response.content.read(64 * 1024)
                if response.status != 200:
                    raise ProxyError(f"Hermes dashboard login failed with HTTP {response.status}")
                try:
                    payload = json.loads(body)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ProxyError("Hermes dashboard login returned an invalid response") from exc
                new_cookies = _extract_auth_set_cookies(response.headers.getall("Set-Cookie", []))
                if payload.get("ok") is not True or not any(
                    _cookie_is_auth(name) and name.endswith("hermes_session_at") and value
                    for name, value in new_cookies.items()
                ):
                    raise ProxyError("Hermes dashboard login did not create an authenticated session")
                self.cookies = {name: value for name, value in new_cookies.items() if value}
                self.cookie_generation += 1
                return self.cookie_generation

    def invalidate_if_unchanged(self, generation: int) -> None:
        if self.cookie_generation == generation:
            self.cookies.clear()
            self.cookie_generation += 1

    def _upstream_cookie_header(self, browser_cookie: str) -> str:
        # The dashboard keeps UI state client-side. No browser cookie is
        # needed upstream; only this proxy's server-side Hermes session is
        # allowed to authenticate requests.
        del browser_cookie
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items())

    def _upstream_headers(self, request: web.Request) -> CIMultiDict[str]:
        connection_tokens = {
            token.strip().lower()
            for value in _header_values(request, b"connection")
            for token in value.split(",")
            if token.strip()
        }
        excluded = HOP_BY_HOP_HEADERS | connection_tokens | FORWARDED_HEADERS | {
            "host", "cookie", "authorization", "content-length", "accept-encoding",
        }
        output: CIMultiDict[str] = CIMultiDict()
        for name, value in request.headers.items():
            if name.lower() not in excluded:
                output.add(name, value)
        cookie = self._upstream_cookie_header(request.headers.get("Cookie", ""))
        if cookie:
            output["Cookie"] = cookie
        output["Host"] = "192.168.1.20:9119"
        output["Accept-Encoding"] = "identity"
        return output

    async def handle(self, request: web.Request) -> web.StreamResponse:
        peer = _peer_ip(request)
        if peer is None or peer not in self.lan_network:
            raise web.HTTPForbidden(text="LAN access required")
        if not _host_is_valid(request):
            raise web.HTTPMisdirectedRequest(text="Unexpected Host")
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY_BYTES, actual_size=request.content_length)
        if request.method not in SAFE_METHODS:
            if not _origin_is_valid(request):
                raise web.HTTPForbidden(text="Same-origin request required")
            fetch_sites = _header_values(request, b"sec-fetch-site")
            if any(value.lower() == "cross-site" for value in fetch_sites):
                raise web.HTTPForbidden(text="Cross-site request rejected")
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if not _origin_is_valid(request):
                raise web.HTTPForbidden(text="Same-origin WebSocket required")
            fetch_sites = _header_values(request, b"sec-fetch-site")
            if any(value.lower() == "cross-site" for value in fetch_sites):
                raise web.HTTPForbidden(text="Cross-site WebSocket rejected")
            return await self.handle_websocket(request)

        path = request.path
        if path == "/login":
            raise web.HTTPFound(location="/engineering-team")
        if path in MUTATING_AUTH_PATHS or path.startswith("/auth/native/") or path.startswith("/auth/login") or path.startswith("/auth/callback"):
            if path == "/auth/logout":
                return web.Response(
                    status=200,
                    text="Hermes LAN session is managed by the local proxy. Close this tab to end access.",
                    content_type="text/plain",
                )
            raise web.HTTPNotFound(text="Interactive authentication is handled by the LAN proxy")
        if (path.startswith("/auth/") or path.startswith("/api/auth/")) and path not in ALLOWED_AUTH_PATHS:
            raise web.HTTPNotFound(text="Authentication route unavailable through LAN proxy")

        try:
            generation = await self.ensure_session()
        except (ProxyError, ClientError, asyncio.TimeoutError) as exc:
            raise web.HTTPServiceUnavailable(
                text="Dashboard authentication is temporarily unavailable"
            ) from exc
        if not self.client or not self.upstream_base_url:
            raise web.HTTPBadGateway(text="Dashboard upstream unavailable")
        url = self.upstream_base_url + request.rel_url.raw_path_qs
        try:
            upstream = await self.client.request(
                request.method,
                url,
                headers=self._upstream_headers(request),
                data=_limited_body(request) if request.can_read_body else None,
                allow_redirects=False,
                skip_auto_headers={"Accept-Encoding"},
            )
        except (ClientError, asyncio.TimeoutError) as exc:
            raise web.HTTPBadGateway(text="Dashboard upstream unavailable") from exc

        updated = _extract_auth_set_cookies(upstream.headers.getall("Set-Cookie", []))
        if updated:
            async with self.auth_lock:
                for name, value in updated.items():
                    if value:
                        self.cookies[name] = value
                    else:
                        self.cookies.pop(name, None)
                self.cookie_generation += 1
        if upstream.status == 401:
            self.invalidate_if_unchanged(generation)
        location = upstream.headers.get("Location")
        if (
            upstream.status in (301, 302, 303, 307, 308)
            and location
            and _redirect_location_is_local(location, self.credentials.public_url)
            and urlsplit(location).path == "/login"
        ):
            # Hermes can send its ordinary login redirect after a restart
            # invalidates the cached cookie. Invalidate this generation and
            # return a local safe redirect. The next GET logs in afresh; this
            # request is never replayed, even if it was a mutation.
            self.invalidate_if_unchanged(generation)
            upstream.release()
            raise web.HTTPFound(location="/engineering-team")
        if upstream.status in (301, 302, 303, 307, 308) and location:
            if not _redirect_location_is_local(location, self.credentials.public_url):
                upstream.release()
                raise web.HTTPBadGateway(text="Blocked external dashboard redirect")

        headers = _filtered_response_headers(upstream.headers)
        if location:
            parsed = urlsplit(location)
            if parsed.scheme or parsed.netloc:
                public = urlsplit(self.credentials.public_url)
                headers["Location"] = parsed.path or "/"
                if parsed.query:
                    headers["Location"] += "?" + parsed.query
                if parsed.fragment:
                    headers["Location"] += "#" + parsed.fragment
        response = web.StreamResponse(status=upstream.status, headers=headers)
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
        except (ConnectionError, asyncio.CancelledError):
            upstream.close()
            raise
        finally:
            upstream.release()
        await response.write_eof()
        return response

    async def handle_websocket(self, request: web.Request) -> web.StreamResponse:
        try:
            generation = await self.ensure_session()
        except (ProxyError, ClientError, asyncio.TimeoutError) as exc:
            raise web.HTTPServiceUnavailable(
                text="Dashboard authentication is temporarily unavailable"
            ) from exc
        if not self.client or not self.upstream_base_url:
            raise web.HTTPBadGateway(text="Dashboard upstream unavailable")
        ws_headers = self._upstream_headers(request)
        for name in tuple(ws_headers):
            if name.lower().startswith("sec-websocket-"):
                del ws_headers[name]
        subprotocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        try:
            upstream = await asyncio.wait_for(
                self.client.ws_connect(
                    self.upstream_base_url + request.rel_url.raw_path_qs,
                    headers=ws_headers,
                    protocols=subprotocols,
                    origin="http://192.168.1.20:9119",
                    autoclose=True,
                    autoping=True,
                    max_msg_size=MAX_WS_MESSAGE_BYTES,
                    timeout=ClientWSTimeout(ws_close=10),
                ),
                timeout=10,
            )
        except WSServerHandshakeError as exc:
            if exc.status == 401:
                self.invalidate_if_unchanged(generation)
            raise web.HTTPBadGateway(text="Dashboard WebSocket unavailable") from exc
        except (ClientError, asyncio.TimeoutError) as exc:
            raise web.HTTPBadGateway(text="Dashboard WebSocket unavailable") from exc
        if upstream.close_code in (4001, 4401):
            self.invalidate_if_unchanged(generation)
        downstream = web.WebSocketResponse(
            protocols=[p for p in upstream.protocol and [upstream.protocol] or []],
            autoping=True,
            heartbeat=30,
            max_msg_size=MAX_WS_MESSAGE_BYTES,
        )
        await downstream.prepare(request)

        async def browser_to_upstream() -> None:
            async for message in downstream:
                if message.type == WSMsgType.TEXT:
                    await upstream.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await upstream.send_bytes(message.data)
                elif message.type == WSMsgType.CLOSE:
                    await upstream.close(code=message.data or 1000, message=message.extra or b"")
                    break
                elif message.type == WSMsgType.ERROR:
                    break

        async def upstream_to_browser() -> None:
            async for message in upstream:
                if message.type == WSMsgType.TEXT:
                    await downstream.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await downstream.send_bytes(message.data)
                elif message.type == WSMsgType.CLOSE:
                    await downstream.close(code=message.data or 1000, message=message.extra or b"")
                    break
                elif message.type == WSMsgType.ERROR:
                    break

        tasks = [
            asyncio.create_task(browser_to_upstream()),
            asyncio.create_task(upstream_to_browser()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            await upstream.close()
            if not downstream.closed:
                await downstream.close()
        return downstream


def create_app(proxy: DashboardProxy) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.cleanup_ctx.append(proxy_context(proxy))
    app.router.add_route("*", "/{tail:.*}", proxy.handle)
    return app


def proxy_context(proxy: DashboardProxy):
    async def context(app: web.Application):
        await proxy.start(app)
        try:
            yield
        finally:
            await proxy.close(app)
    return context


def main() -> None:
    bind_host = os.environ.get("HERMES_DASHBOARD_BIND", "192.168.1.20")
    lan_cidr = os.environ.get("HERMES_DASHBOARD_LAN_CIDR", "192.168.1.0/24")
    credentials_path = os.environ.get(
        "HERMES_DASHBOARD_CREDENTIALS",
        "/home/alex/apps/hermes-vpn/.dashboard-proxy-credentials.json",
    )
    network = validate_lan(bind_host, lan_cidr)
    credentials = load_credentials(credentials_path)
    proxy = DashboardProxy(credentials, network)
    web.run_app(create_app(proxy), host=bind_host, port=PUBLIC_PORT, access_log=None)


if __name__ == "__main__":
    main()
