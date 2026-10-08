"""spec 05 §6: gateway routing — pass-through requests bypass the scheduler, and
a pass-through request is denied immediately (503) while the app is stopped.

The denial is state-conditional: with the app running the same pass-through
request is forwarded (never wakes the app, never touches the scheduler).
"""

from __future__ import annotations

import asyncio

from gpusybio.applications.lifecycle import ProcessState
from gpusybio.gateway.proxy import make_gateway_proxy
from gpusybio.state import StateManager


def _make_proxy(app_state: StateManager, should_schedule):
    return make_gateway_proxy(
        gateway={"id": "gw1", "application_id": "app1", "enabled": True},
        request_manager=None,
        session=None,
        app_state=app_state,
        app_cfg={"id": "app1", "port": 7777, "max_concurrent_requests": 8},
        should_schedule=should_schedule,
    )


def _drive(proxy, method: str = "GET", path: str = "/index.html") -> list:
    """Run one HTTP request through the ASGI app and return the sent messages."""
    sent: list = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    asyncio.run(proxy(scope, receive, send))
    return sent


def test_pass_through_denied_when_app_stopped():
    """spec 05 §6: a pass-through request is denied immediately while the
    application is stopped — no forward is attempted, no scheduler involved."""
    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STOPPED)
    # /api/* is scheduled; /index.html passes through.
    proxy = _make_proxy(app_state, should_schedule=lambda m, p: p.startswith("/api/"))

    sent = _drive(proxy, method="GET", path="/index.html")
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 503
    assert sent[-1]["body"] == b"application stopped"


def test_pass_through_forwarded_when_app_running():
    """spec 05 §6: the stopped-app denial is state-conditional — with the app
    running, the same pass-through request is forwarded (not denied). Here it
    only fails with 502 because there is no live upstream, never 503."""
    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STARTED)
    proxy = _make_proxy(app_state, should_schedule=lambda m, p: p.startswith("/api/"))

    sent = _drive(proxy, method="GET", path="/index.html")
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 502  # upstream unreachable, not the stopped rejection


def test_scheduled_request_not_denied_when_stopped():
    """spec 05 §6: the stopped rejection applies only to pass-through requests;
    a scheduled request goes on to the scheduler (its intake is reached)."""
    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STOPPED)

    class _Slot:
        request_id = "req-1"
        deadline = None

    class _RM:
        intake_reached = False

        async def receive(self, gateway_id, metadata=None):
            self.intake_reached = True
            return _Slot()

        async def begin_forward(self, request_id):
            return "app1"

        async def complete(self, *a, **k):
            return None

    rm = _RM()
    proxy = make_gateway_proxy(
        gateway={"id": "gw1", "application_id": "app1", "enabled": True},
        request_manager=rm,
        session=None,
        app_state=app_state,
        app_cfg={"id": "app1", "port": 7777, "max_concurrent_requests": 8},
        should_schedule=lambda m, p: p == "/api/prompt",
    )

    sent = _drive(proxy, method="POST", path="/api/prompt")
    # The request went to the scheduler (intake reached), so it is NOT the
    # stopped rejection — it proceeds to the forward (502, no live upstream).
    assert rm.intake_reached is True
    assert sent[0]["status"] == 502


def test_origin_referer_rewritten_to_upstream():
    """spec 05 §6: the gateway rewrites origin/referer to the upstream's own
    origin, so the app sees a same-origin request. The browser sends the
    gateway's origin (a different port), which the app rejects as cross-origin
    (403); the other headers pass through untouched."""
    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STARTED)
    captured = {}

    class _Resp:
        status = 200
        headers = {"Content-Length": "0"}

        async def read(self):
            return b""

        def release(self):
            pass

        def close(self):
            pass

    class _Session:
        async def request(self, method, target, data=None, headers=None):
            captured["headers"] = {k.lower(): v for k, v in headers}
            return _Resp()

    proxy = make_gateway_proxy(
        gateway={"id": "gw1", "application_id": "app1", "enabled": True},
        request_manager=None,
        session=_Session(),
        app_state=app_state,
        app_cfg={"id": "app1", "port": 7777, "max_concurrent_requests": 8},
        should_schedule=lambda m, p: p.startswith("/api/"),
    )

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/index.html",
        "headers": [
            (b"origin", b"http://127.0.0.1:38188"),
            (b"referer", b"http://127.0.0.1:38188/"),
            (b"user-agent", b"test"),
        ],
        "query_string": b"",
    }
    sent: list = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(proxy(scope, receive, send))

    # origin/referer are rewritten to the upstream's origin; host is set; and
    # unrelated headers pass through untouched.
    assert captured["headers"]["origin"] == "http://127.0.0.1:7777"
    assert captured["headers"]["referer"] == "http://127.0.0.1:7777"
    assert captured["headers"]["host"] == "127.0.0.1:7777"
    assert captured["headers"]["user-agent"] == "test"


def test_encoded_slash_forwarded_verbatim():
    """Regression: the upstream URL must preserve percent-encoded slashes
    (%2F) inside a path segment. scope["path"] is percent-decoded, so building
    the target from it turned workflows%2Ffoo.json into workflows/foo.json —
    which breaks the upstream route (ComfyUI /api/userdata/{file} matches the
    raw path and 404s on a literal /). uvicorn exposes the raw form in
    scope["raw_path"] (bytes); forward it verbatim."""
    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STARTED)
    captured = {}

    class _Resp:
        status = 200
        headers = {"Content-Length": "0"}

        async def read(self):
            return b""

        def release(self):
            pass

        def close(self):
            pass

    class _Session:
        async def request(self, method, target, data=None, headers=None):
            captured["target"] = target
            return _Resp()

    proxy = make_gateway_proxy(
        gateway={"id": "gw1", "application_id": "app1", "enabled": True},
        request_manager=None,
        session=_Session(),
        app_state=app_state,
        app_cfg={"id": "app1", "port": 7777, "max_concurrent_requests": 8},
        should_schedule=lambda m, p: p.startswith("/api/"),
    )
    # uvicorn semantics: "path" decoded, "raw_path" the raw bytes. The path is
    # a pass-through (should_schedule False) so it takes _forward_direct.
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/userdata/workflows/foo.json",
        "raw_path": b"/userdata/workflows%2Ffoo.json",
        "headers": [],
        "query_string": b"",
    }
    sent: list = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(proxy(scope, receive, send))

    assert captured["target"] == "http://127.0.0.1:7777/userdata/workflows%2Ffoo.json"
    assert sent[0]["status"] == 200


def test_stream_pump_survives_body_lines_longer_than_512kib():
    """Regression: the pump must deliver bodies containing lines longer than
    512KiB in full. The old pump did `async for chunk in resp.content`, which
    iterates line-wise; aiohttp's readline() raises LineTooLong for any line
    longer than 2x the client limit (512KiB default). That silently truncated
    real responses (ComfyUI's vendor bundle embeds multi-MiB base64 WASM
    without newlines) and left the chunked stream unterminated — the browser
    saw 200 OK plus ERR_INCOMPLETE_CHUNKED_ENCODING at the first long line."""
    import aiohttp
    from aiohttp import web

    long_line = b"x" * (600 * 1024)  # over the 512KiB line limit
    body = b"head\n" + long_line + b"\ntail\n"

    async def handler(request):
        return web.Response(body=body, content_type="application/octet-stream")

    app = web.Application()
    app.router.add_get("/big", handler)

    async def run():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        app_state = StateManager()
        app_state.set_process("app1", ProcessState.STARTED)
        session = aiohttp.ClientSession()
        try:
            proxy = make_gateway_proxy(
                gateway={"id": "gw1", "application_id": "app1", "enabled": True},
                request_manager=None,
                session=session,
                app_state=app_state,
                app_cfg={"id": "app1", "port": port, "max_concurrent_requests": 8},
                should_schedule=lambda m, p: p.startswith("/api/"),
            )
            sent: list = []

            async def send(msg):
                sent.append(msg)

            started = False

            async def receive():
                # First call: the request itself; afterwards block (no
                # disconnect), matching uvicorn's receive() semantics.
                nonlocal started
                if not started:
                    started = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Event().wait()

            scope = {
                "type": "http",
                "method": "GET",
                "path": "/big",
                "headers": [],
                "query_string": b"",
            }
            await proxy(scope, receive, send)
        finally:
            await session.close()
            await site.stop()
            await runner.cleanup()

        assert sent[0]["status"] == 200
        data = b"".join(m["body"] for m in sent if m["type"] == "http.response.body")
        assert data == body  # full body, including the 600KiB line
        assert sent[-1] == {"type": "http.response.body", "body": b""}  # clean end

    asyncio.run(run())
