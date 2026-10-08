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
from gpusybio.metrics.type_registry import make_type_registry
from gpusybio.application_types.llama import LlamaHandler
from gpusybio.metrics.requests import RequestMetrics
from gpusybio.metrics.typespec import COMMON_METRICS


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


# -- unit: raw-view merge (spec 08 §4) -------------------------------------------
def test_merge_metrics_log_derived_wins_over_zeroed_bucket():
    """The bucket snapshot always carries input/output/total_tokens (zero on a
    fresh process); it must not stomp the tailer's lifetime totals."""
    from gpusybio.metrics import _merge_metrics
    type_metrics = {"input_tokens": 3600329, "output_tokens": 378120,
                    "total_tokens": 3978449, "requests": 207,
                    "draft_acceptance": 43.8}
    request = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
               "requests_received": 4, "requests_failed": 0, "p50_wait_sec": 2.5,
               "active_requests": 0, "queued_requests": 0}
    out = _merge_metrics({}, type_metrics, request, {})
    assert out["input_tokens"] == 3600329  # not stomped by the bucket's 0
    assert out["output_tokens"] == 378120
    assert out["total_tokens"] == 3978449
    assert out["requests"] == 207
    assert out["draft_acceptance"] == 43.8
    # request-unique keys still fill in (including zero-valued counters)
    assert out["requests_received"] == 4
    assert out["requests_failed"] == 0
    assert out["p50_wait_sec"] == 2.5


def test_merge_metrics_request_fills_empty_tailer():
    """With an empty tailer (all zeros) the request-tracked tokens fill in."""
    from gpusybio.metrics import _merge_metrics
    type_metrics = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    request = {"input_tokens": 16, "output_tokens": 32, "total_tokens": 48,
               "requests_completed": 1}
    out = _merge_metrics({}, type_metrics, request, {})
    assert out["input_tokens"] == 16
    assert out["output_tokens"] == 32
    assert out["total_tokens"] == 48
    assert out["requests_completed"] == 1


# -- unit: type contract (spec 08 §9) -------------------------------------------
def test_type_registry_builtin_and_custom():
    import types

    reg = make_type_registry()
    assert reg.spec_for("llama").type == "llama"
    assert "input_tokens" in [m.id for m in reg.spec_for("llama").metrics]
    assert "total_tokens" not in [m.id for m in reg.spec_for("llama").metrics]  # dropped
    # unknown type falls back to generic
    assert reg.spec_for("nonsense").type == "generic"
    # custom reads its metric list from the app config (description JSON)
    import json

    row = types.SimpleNamespace(description=json.dumps({"metrics": [
        {"id": "queue_depth", "name": "Queue depth", "unit": "req", "kind": "gauge",
         "source": "handler", "display": "card",
         "collect": {"url": "/stats", "json_path": "queue.depth"}}
    ]}))
    spec = reg.spec_for("custom", row)
    assert spec.type == "custom"
    assert spec.metric("queue_depth").collect["url"] == "/stats"
    # custom with no metrics falls back to generic
    assert reg.spec_for("custom", types.SimpleNamespace(description=None)).type == "generic"


def test_should_schedule_routing():
    """spec 05 §6: the application type declares which requests are scheduled,
    as an easily-modifiable array of path regexes."""
    from gpusybio.metrics.typespec import TypeSpec
    from gpusybio.application_types.comfyui import COMFYUI_SPEC
    # comfyui: only /api/prompt (queue submissions) and the naked root "/" are
    # scheduled; other /api/* reads pass through
    assert COMFYUI_SPEC.should_schedule("POST", "/api/prompt") is True
    assert COMFYUI_SPEC.should_schedule("GET", "/api/system_stats") is False
    assert COMFYUI_SPEC.should_schedule("GET", "/api/queue") is False
    assert COMFYUI_SPEC.should_schedule("GET", "/") is True
    # everything else (frontend static, other reads) passes through
    assert COMFYUI_SPEC.should_schedule("GET", "/index.html") is False
    assert COMFYUI_SPEC.should_schedule("GET", "/ws") is False
    # default type: everything is scheduled
    assert TypeSpec("llama").should_schedule("POST", "/api/chat") is True
    assert TypeSpec("generic").should_schedule("GET", "/anything") is True


def test_common_metrics_present():
    ids = [m.id for m in COMMON_METRICS]
    assert "p95_wait_sec" in ids
    assert "p99_service_sec" in ids
    assert "requests_failed" in ids
    assert "avg_wait_sec" in ids
    # reduced request blocks: active/queued are shown combined as "Active" in the UI
    assert "active_requests" not in ids
    assert "requests_total" not in ids


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


def test_summary_reads_only_last_two_minutes(tmp_path):
    """summary() must not materialize the 30-day retention column: only rows
    from the last 120 s feed the UI triplets (spec 08 §6)."""
    engine = create_engine(str(tmp_path / "t.db"))
    init_db(engine)
    db = make_session_factory(engine)
    from datetime import datetime, timedelta, timezone

    from gpusybio.database.models import MetricAggregate

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db() as s:
        # fresh row (last minute) + stale row (30 days ago, pre-dates retention)
        s.add(MetricAggregate(scope="gpu", metric="gpu_pct", window_sec=60,
                             min=40.0, max=60.0, avg=50.0, count=60,
                             at=now - timedelta(seconds=30)))
        s.add(MetricAggregate(scope="gpu", metric="gpu_pct", window_sec=60,
                             min=1.0, max=2.0, avg=1.5, count=60,
                             at=now - timedelta(days=20)))
        s.add(MetricAggregate(scope="a1", metric="active_requests", window_sec=60,
                             min=0.0, max=1.0, avg=0.5, count=60,
                             at=now - timedelta(seconds=45)))

    out = MetricsAggregator(db, ["a1"]).summary()
    assert out["gpu"]["gpu_pct"]["average"] == 50.0  # fresh row wins
    assert out["gpu"]["gpu_pct"]["maximum"] == 60.0
    assert out["applications"]["a1"]["active_requests"]["average"] == 0.5


def test_aggregator_prune_event_tables(tmp_path):
    """Event tables are retained 7 days (spec 08 §6) — they grow unbounded
    otherwise (scheduler persists one row per reconcile tick)."""
    engine = create_engine(str(tmp_path / "t.db"))
    init_db(engine)
    db = make_session_factory(engine)
    from datetime import datetime, timedelta, timezone

    from gpusybio.database.models import ApplicationEvent, GatewayEvent, SchedulerEvent

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db() as s:
        s.add(ApplicationEvent(application_id="a1", event_type="T_OLD", at=now - timedelta(days=8)))
        s.add(ApplicationEvent(application_id="a1", event_type="T_NEW", at=now - timedelta(days=1)))
        s.add(GatewayEvent(gateway_id="gw1", event_type="G_OLD", at=now - timedelta(days=8)))
        s.add(SchedulerEvent(trigger="t", decision="D", at=now - timedelta(days=8)))

    MetricsAggregator(db, []).prune()
    with db() as s:
        assert s.query(ApplicationEvent).count() == 1  # only the 1-day-old row
        assert s.query(ApplicationEvent).first().event_type == "T_NEW"
        assert s.query(GatewayEvent).count() == 0
        assert s.query(SchedulerEvent).count() == 0


# -- unit: active time (spec 08 §7) ---------------------------------------------
def test_active_time_tracking():
    from gpusybio.metrics import MetricsCollector
    from gpusybio.state import StateManager
    from gpusybio.applications.lifecycle import ProcessState, ResidencyState

    states = StateManager()
    s = states.ensure("a1")
    s.process = ProcessState.STARTED
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
    assert "requests_failed" in ids  # common metric
    assert "active_requests" in m["state"]  # combined into "Active" in the UI
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
    assert "input_tokens" in spec_ids
    assert "cached_tokens" in spec_ids
    assert "draft_acceptance" in spec_ids
    assert "cache_hit_pct" in spec_ids
    # the reduced request blocks: total_tokens and completed_requests are dropped
    assert "total_tokens" not in spec_ids
    assert "completed_requests" not in spec_ids
    await c.aclose()


async def test_metrics_endpoint_token_totals_not_stomped_by_bucket(server):
    """The endpoint's raw token totals come from the log tailer (lifetime), not
    the request bucket's zeroed counts (spec 08 §4/§5.1). A fresh process's
    bucket has input/output/total_tokens=0; it must not override the tailer."""
    log = f"/tmp/gpusybio-e2e-data-{os.getpid()}/logs/a1.log"
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a") as f:
        f.write(
            "I slot print_timing: id 0 | task 1 | prompt eval time =   1234.56 ms /   100 tokens "
            "(   12.35 ms per token,   81.00 tokens per second)\n"
            "I slot print_timing: id 0 | task 1 |       total time =   2345.67 ms /   150 tokens\n"
        )
    await asyncio.sleep(2.5)  # let the 1 s metrics loop consume the log lines
    c = httpx.AsyncClient()
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a1/metrics")
    assert r.status_code == 200
    raw = r.json()["raw"]
    assert raw["input_tokens"] == 100  # tailer value, not the bucket's 0
    assert raw["total_tokens"] == 150
    # throughput-weighted: 100 tokens / 1234.56 ms (the line's rounded "81.00"
    # is not what's used)
    assert raw["prompt_tokens_per_sec"] == pytest.approx(81.0, abs=0.01)
    await c.aclose()


@pytest.fixture
def cfg_overrides():
    return {"application_types": {"a1": "llama"}}
