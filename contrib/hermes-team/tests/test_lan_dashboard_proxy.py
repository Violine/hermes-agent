import asyncio
import ipaddress
import json
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lan_dashboard_proxy as proxy_module  # noqa: E402


class LanDashboardProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.credential_path = Path(self.tmp.name) / "dashboard.json"
        self.credential_path.write_text(json.dumps({
            "url": "http://192.168.1.20:9119/",
            "username": "test-user",
            "password": "test-password",
        }))
        self.credential_path.chmod(0o600)
        self.login_count = 0
        self.login_failure = False
        self.mutation_count = 0
        self.seen_cookie = None
        self.seen_forwarded = None
        self.seen_auth = None
        self.seen_host = None
        self.set_cookie_seen_by_browser = None

        async def login(request):
            body = await request.json()
            self.assertEqual(body["provider"], "basic")
            self.assertEqual(body["username"], "test-user")
            self.assertEqual(body["password"], "test-password")
            self.login_count += 1
            if self.login_failure:
                return web.json_response({"detail": "sensitive upstream diagnostic"}, status=503)
            response = web.json_response({"ok": True, "next": "/engineering-team"})
            response.set_cookie("hermes_session_at", "server-session", httponly=True)
            response.set_cookie("hermes_session_rt", "server-refresh", httponly=True)
            return response

        async def echo(request):
            self.seen_cookie = request.headers.get("Cookie", "")
            self.seen_forwarded = {
                name: request.headers.get(name)
                for name in ("Forwarded", "X-Forwarded-For", "X-Real-IP")
            }
            self.seen_auth = request.headers.get("Authorization")
            self.seen_host = request.headers.get("Host")
            if "hermes_session_at=server-session" not in self.seen_cookie:
                return web.json_response({"error": "unauthenticated"}, status=401)
            response = web.json_response({"ok": True})
            response.set_cookie("hermes_session_at", "must-not-leak")
            return response

        async def stale_session(request):
            return web.Response(status=302, headers={"Location": "/login?next=%2Fengineering-team"})

        async def stream(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            await asyncio.sleep(0.01)
            await response.write(b"data: second\n\n")
            await response.write_eof()
            return response

        async def mutate(request):
            await request.read()
            self.mutation_count += 1
            return web.json_response({"error": "unauthenticated"}, status=401)

        async def websocket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await ws.send_str("echo:" + msg.data)
                    break
            return ws

        upstream_app = web.Application()
        upstream_app.router.add_post("/auth/password-login", login)
        upstream_app.router.add_get("/api/echo", echo)
        upstream_app.router.add_get("/api/stale", stale_session)
        upstream_app.router.add_get("/api/events", stream)
        upstream_app.router.add_post("/api/mutate", mutate)
        upstream_app.router.add_get("/api/ws", websocket)
        self.upstream = TestServer(upstream_app)
        await self.upstream.start_server()

        creds = proxy_module.load_credentials(self.credential_path)
        # The test server's TCP peer is loopback; production defaults remain
        # restricted to the physical LAN CIDR.
        network = ipaddress.ip_network("127.0.0.0/8")
        proxy = proxy_module.DashboardProxy(
            creds,
            network,
            upstream_base_url=str(self.upstream.make_url("/")).rstrip("/"),
        )
        self.client = TestClient(TestServer(proxy_module.create_app(proxy)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.tmp.cleanup()

    def _headers(self, **extra):
        return {"Host": "192.168.1.20:9119", **extra}

    async def test_dashboard_session_is_server_side_and_client_cookie_is_not_auth(self):
        response = await self.client.get(
            "/api/echo",
            headers=self._headers(
                Cookie="hermes_session_at=attacker; harmless=kept",
                Authorization="Bearer attacker",
                Forwarded="for=192.168.1.10",
                **{"X-Forwarded-For": "192.168.1.10", "X-Real-IP": "192.168.1.10"},
            ),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"ok": True})
        self.assertEqual(self.login_count, 1)
        self.assertIn("hermes_session_at=server-session", self.seen_cookie)
        self.assertNotIn("harmless=kept", self.seen_cookie)
        self.assertNotIn("attacker", self.seen_cookie)
        self.assertEqual(self.seen_auth, None)
        self.assertEqual(self.seen_host, "192.168.1.20:9119")
        self.assertEqual(self.seen_forwarded, {"Forwarded": None, "X-Forwarded-For": None, "X-Real-IP": None})
        self.assertFalse(any(k.lower() == "set-cookie" for k in response.headers))
        self.assertNotIn("must-not-leak", str(response.headers))

    async def test_backend_login_redirect_invalidates_session_without_replaying_request(self):
        response = await self.client.get(
            "/api/stale",
            headers=self._headers(Cookie="hermes_session_at=old; harmless=ignored"),
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/engineering-team")
        self.assertEqual(self.login_count, 1)
        self.assertEqual(self.mutation_count, 0)

        response = await self.client.get("/api/echo", headers=self._headers(), allow_redirects=False)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.login_count, 2)

    async def test_upstream_login_failure_returns_generic_503(self):
        self.login_failure = True
        response = await self.client.get("/api/echo", headers=self._headers())
        self.assertEqual(response.status, 503)
        body = await response.text()
        self.assertIn("temporarily unavailable", body)
        self.assertNotIn("sensitive", body)
        self.assertEqual(self.login_count, 1)

    async def test_rejects_wrong_host(self):
        response = await self.client.get("/api/echo", headers={"Host": "attacker.example"})
        self.assertEqual(response.status, 421)
        self.assertEqual(self.login_count, 0)

    async def test_rejects_non_lan_socket_peer(self):
        restricted = proxy_module.DashboardProxy(
            proxy_module.load_credentials(self.credential_path),
            ipaddress.ip_network("192.168.1.0/24"),
            upstream_base_url=str(self.upstream.make_url("/")).rstrip("/"),
        )
        client = TestClient(TestServer(proxy_module.create_app(restricted)))
        await client.start_server()
        try:
            response = await client.get("/api/echo", headers=self._headers())
            self.assertEqual(response.status, 403)
            self.assertEqual(self.login_count, 0)
        finally:
            await client.close()

    async def test_mutation_requires_exact_origin_and_cross_site_is_rejected(self):
        response = await self.client.post(
            "/api/echo", headers=self._headers(), data="{}"
        )
        self.assertEqual(response.status, 403)
        response = await self.client.post(
            "/api/echo",
            headers=self._headers(
                Origin="http://192.168.1.20:9119",
                **{"Sec-Fetch-Site": "cross-site"},
            ),
            data="{}",
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.login_count, 0)

    async def test_uncertain_mutation_is_never_retried_after_upstream_401(self):
        response = await self.client.post(
            "/api/mutate",
            headers=self._headers(
                Origin="http://192.168.1.20:9119",
                **{"Sec-Fetch-Site": "same-origin"},
            ),
            data="change",
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(self.mutation_count, 1)
        self.assertEqual(self.login_count, 1)

    async def test_rejects_cross_origin_websocket(self):
        with self.assertRaises(Exception):
            await self.client.ws_connect(
                "/api/ws",
                headers=self._headers(
                    Origin="http://evil.example",
                    **{"Sec-Fetch-Site": "cross-site"},
                ),
            )
        self.assertEqual(self.login_count, 0)

    async def test_login_route_redirects_and_interactive_logout_does_not_revoke_session(self):
        response = await self.client.get("/login", headers=self._headers(), allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/engineering-team")
        response = await self.client.post(
            "/auth/logout",
            headers=self._headers(
                Origin="http://192.168.1.20:9119",
                **{"Sec-Fetch-Site": "same-origin"},
            ),
        )
        self.assertEqual(response.status, 200)
        self.assertIn("managed by the local proxy", await response.text())
        self.assertEqual(self.login_count, 0)

    async def test_sse_is_streamed_and_websocket_frames_are_relayed(self):
        response = await self.client.get("/api/events", headers=self._headers())
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "data: first\n\ndata: second\n\n")
        ws = await self.client.ws_connect(
            "/api/ws",
            headers=self._headers(
                Origin="http://192.168.1.20:9119",
                **{"Sec-Fetch-Site": "same-origin"},
            ),
        )
        await ws.send_str("hello")
        message = await ws.receive(timeout=2)
        self.assertEqual(message.data, "echo:hello")
        await ws.close()

    async def test_credentials_require_mode_0600_and_fixed_origin(self):
        path = Path(self.tmp.name) / "unsafe.json"
        path.write_text(json.dumps({
            "url": "http://192.168.1.20:9119/",
            "username": "u",
            "password": "p",
        }))
        path.chmod(0o640)
        with self.assertRaises(proxy_module.ProxyError):
            proxy_module.load_credentials(path)

        path.chmod(0o600)
        path.write_text(json.dumps({
            "url": "http://attacker.example:9119/",
            "username": "u",
            "password": "p",
        }))
        with self.assertRaises(proxy_module.ProxyError):
            proxy_module.load_credentials(path)


if __name__ == "__main__":
    unittest.main()
