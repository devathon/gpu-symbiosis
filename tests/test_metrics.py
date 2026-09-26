"""Phase 3 — Metrics (spec 08): request/latency metrics, type-metrics contract,
aggregation/retention, active time, and the metrics API.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest
from conftest import ADMIN_PORT

from gpusybio.database.db import create_engine, init_db, make_session_factory
from gpusybio.database.models import MetricSample, Application, Gateway
from gpusybio.events.bus import EventBus
from gpusybio.metrics.aggregate import MetricsAggregator
from gpusybio.metrics.handlers import LlamaHandler, make_handler
from gpusybio.metrics.requests import RequestMetrics
from gpusybio.metrics.typespec import COMMON_METRICS, LLAMA_SPEC, TypeRegistry


# -- unit: request metrics (spec 08 §2/§4) ------------------------------------
class FakeRow:
    def __init__(self, gateway_id, wait, service, in_tok=None, out_tok=None):
        self.gateway_id = gateway_id
        self.queue_wait_sec = wait
        self.service_time_sec = service
        self.input_tokens = in_tok
        self.output_tokens = out_tok


class FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, model, rid):
        return self._rows.get(rid)


def test_request_metrics_latency_and_counts():
    rm = RequestMetrics()
    b = rm._app_bucket("a1")
    now = time.time()
    # 4 completed requests: wait 1,2,3,4s; service 0.5,1.0,1.5,2.0s
    for i in range(1, 5):
        rm._push(b.arrivals, now - i)  # simulate the receive events
        rm._record_terminal(b, "COMPLETED", now, wait=float(i), service=float(i) / 2,
                           in_tok=10, out_tok=5)
    b.cancelled += 1
    snap = rm.app("a1", active=2, queued=3)
    assert snap["requests_completed"] == 4
    assert snap["requests_cancelled"] == 1
    assert snap["active_requests"] == 2
    assert snap["queued_requests"] == 3
    assert snap["input_tokens"] == 40
    assert snap["output_tokens"] == 20
    assert snap["total_tokens"] == 60
    # latency percentiles over [1,2,3,4] (linear interpolation)
    assert snap["p50_wait_sec"] == 2.5
    assert snap["p95_wait_sec"] == 3.85
    assert snap["p99_wait_sec"] == 3.97
    assert snap["average_wait_sec"] == 2.5
    assert snap["average_service_sec"] == 1.25  # mean of [0.5,1.0,1.5,2.0]
    # arrival rate ~ 4/min over the 60s window
    assert snap["arrival_rate"] > 0


def test_request_metrics_db_lookup():
    rm = RequestMetrics(
        db=FakeDB({"r1": FakeRow("gw-a1", wait=1.0, service=2.0, in_tok=16, out_tok=32)})
    )
    ev = __import__("gpusybio.events.bus", fromlist=["Event"]).Event(
        type="request_complete", data={"request_id": "r1", "status": "COMPLETED",
                                       "application_id": "a1"}, at=time.time()
    )
    asyncio.run(rm._on_complete(ev))
    snap = rm.app("a1")
    assert snap["requests_completed"] == 1
    assert snap["input_tokens"] == 16
    assert snap["output_tokens"] == 32
    assert snap["average_wait_sec"] == 1.0
    assert snap["average_service_sec"] == 2.0


# -- unit: type contract (spec 08 §9) -------------------------------------------
def test_type_registry_builtin_and_custom():
    reg = TypeRegistry()
    assert reg.spec_for("llama").type == "llama"
    assert "total_tokens" in [m.id for m in reg.spec_for("llama").metrics]
    # unknown type falls back to generic
    assert reg.spec_for("nonsense").type == "generic"
    # custom reads its metric list
    custom = [
        {"id": "queue_depth", "name": "Queue depth", "unit": "req", "kind": "gauge",
         "source": "handler", "display": "card", "collect": {"url": "/stats", "json_path": "queue.depth"}}
    ]
    spec = reg.spec_for("custom", custom)
    assert spec.type == "custom"
    assert spec.metric("queue_depth").collect["url"] == "/stats"
    # custom with no metrics falls back to generic
    assert reg.spec_for("custom", None).type == "generic"


def test_common_metrics_present():
    ids = [m.id for m in COMMON_METRICS]
    assert "p95_wait_sec" in ids
    assert "p99_service_sec" in ids
    assert "active_requests" in ids
    assert "requests_total" in ids
    assert "avg_wait_sec" in ids


# -- unit: llama handler (spec 08 §5) ------------------------------------------
async def test_llama_handler_token_metrics():
    # Spin a tiny /stats server.
    from aiohttp import web

    async def stats(request):
        return web.json_response({
            "requests": 5, "completed_requests": 4, "failed_requests": 1,
            "input_tokens": 100, "output_tokens": 200, "total_tokens": 300,
        })

    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = web.Application()
    app.router.add_get("/stats", stats)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    from gpusybio.resources.manager import ResourceManager
    from gpusybio.state import StateManager

    rm = RequestMetrics()
    states = StateManager()
    states.ensure("a1")
    # Stub resources (no real GPU needed for /stats collection).
    import types

    res = types.SimpleNamespace(
        observed_mb=lambda app_id: 1000.0,
        gpu_activity_threshold_pct=5.0,
        backend=types.SimpleNamespace(process_utilization_pct=lambda pid: 0.0),
    )
    handler = LlamaHandler(res, states, rm, {"a1": f"http://127.0.0.1:{port}"})
    import aiohttp

    async with aiohttp.ClientSession() as session:
        handler._session = session
        first = await handler.collect_metrics("a1")
        assert first["total_tokens"] == 300
        assert first["requests"] == 5
        # second call computes rates from the delta (same values → rate 0)
        second = await handler.collect_metrics("a1")
        assert second["tokens_per_sec"] >= 0
    await runner.cleanup()
    assert handler.get_request_metrics("a1")["active_requests"] == 0


# -- unit: aggregator + retention (spec 08 §6) ----------------------------------
def test_aggregator_rollup_and_prune(tmp_path):
    engine = create_engine(str(tmp_path / "t.db"))
    init_db(engine)
    db = make_session_factory(engine)
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db() as s:
        s.add(Application(id="a1", name="a1", start_script="x", port=1,
                         configured_gpu_memory_mb=100))
        s.add(Gateway(id="gw1", name="gw1", port=100, application_id="a1"))
        # raw samples comfortably inside the 60s window (now-10s .. now-1s)
        for i in range(10):
            s.add(MetricSample(gpu_pct=50.0 + i, gpu_mem_mb=1000.0 + i * 10,
                              gpu_temp_c=60.0, gpu_power_w=200.0,
                              at=now - timedelta(seconds=10 - i)))
        s.add(MetricSample(application_id="a1", gpu_pct=30.0, gpu_mem_mb=500.0,
                           active_requests=1, at=now - timedelta(seconds=2)))

    agg = MetricsAggregator(db, ["a1"])
    agg.roll_up()
    # 1-minute aggregates created for gpu + app scopes
    with db() as s:
        rows = s.query(__import__("gpusybio.database.models", fromlist=["MetricAggregate"]).MetricAggregate).all()
        scopes = {r.scope for r in rows}
        assert "gpu" in scopes
        assert "a1" in scopes
        # min/max/avg computed
        gpu_mem = [r for r in rows if r.scope == "gpu" and r.metric == "gpu_mem_mb"][0]
        assert gpu_mem.min == 1000.0
        assert gpu_mem.max == 1090.0
        assert gpu_mem.avg is not None

    # prune: insert an old raw sample (25h ago) and verify it's removed
    with db() as s:
        s.add(MetricSample(gpu_pct=1.0, gpu_mem_mb=1.0, at=now - timedelta(hours=25)))
    agg.prune()
    with db() as s:
        old = s.query(MetricSample).filter(MetricSample.gpu_mem_mb == 1.0).count()
        assert old == 0  # pruned (24h retention)


# -- unit: active time (spec 08 §7) ---------------------------------------------
def test_active_time_tracking():
    from gpusybio.metrics import MetricsCollector
    from gpusybio.state import StateManager
    from gpusybio.applications.lifecycle import ProcessState, ResidencyState

    states = StateManager()
    s = states.ensure("a1")
    s.process = ProcessState.RUNNING
    s.residency = ResidencyState.LOADED
    s.active_requests = 1

    mc = MetricsCollector.__new__(MetricsCollector)  # skip __init__
    mc._active_time = {}
    # processing (active requests > 0)
    mc._record_active_time("a1", s, 10.0)
    assert mc._active_time["a1"]["total_active_time"] == 10.0
    assert mc._active_time["a1"]["processing_time"] == 10.0
    # idle loaded (no active requests)
    s.active_requests = 0
    mc._record_active_time("a1", s, 5.0)
    assert mc._active_time["a1"]["idle_loaded_time"] == 5.0
    assert mc._active_time["a1"]["total_active_time"] == 15.0
    # not loaded → no accumulation
    s.residency = ResidencyState.OFF
    before = mc._active_time["a1"]["total_active_time"]
    mc._record_active_time("a1", s, 5.0)
    assert mc._active_time["a1"]["total_active_time"] == before


# -- E2E: metrics API (spec 08 §9.3) -------------------------------------------
async def test_metrics_api(server):
    c = httpx.AsyncClient()
    # a1 is resident (start-loaded). Wait for a sample.
    await asyncio.sleep(2.5)
    # application metrics
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a1/metrics?window=1h")
    assert r.status_code == 200
    m = r.json()
    assert m["application_id"] == "a1"
    assert "metrics" in m and m["metrics"]
    ids = [x["id"] for x in m["metrics"]]
    assert "active_requests" in ids  # common metric
    assert "total_tokens" in m["raw"] or "gpu_memory_mb" in m["raw"]
    # request metrics endpoint
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/metrics/request")
    assert r.status_code == 200
    body = r.json()
    assert "applications" in body and "gateways" in body
    # summary
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/metrics/summary")
    assert r.status_code == 200
    await c.aclose()


async def test_metrics_llama_type(server):
    """With application_type=llama, the type spec is exposed for auto-rendering."""
    c = httpx.AsyncClient()
    await asyncio.sleep(2.5)
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a1/metrics")
    assert r.status_code == 200
    m = r.json()
    assert m["type"] == "llama"
    spec_ids = [x["id"] for x in m["spec"]]
    assert "total_tokens" in spec_ids
    await c.aclose()


@pytest.fixture
def cfg_overrides():
    return {"application_types": {"a1": "llama"}}
