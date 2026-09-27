"""E2E: llama token metrics from the application log (spec 08 §5).

The application's stdout is the log (the start script redirects it to
``<data_path>/logs/<id>.log``). The metrics handler tails the log and parses
the per-request token lines. The gateway also records per-request token counts
from the upstream response headers. This test verifies the end-to-end path:

1. the pid file lands at ``<data_path>/pids/<id>.pid`` (the data-path convention),
2. the log lands at ``<data_path>/logs/<id>.log`` and contains the token lines,
3. the metrics API exposes non-zero token metrics (from the log),
4. the request row records the per-request token counts (from the headers).
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest
from conftest import ADMIN_PORT, A1_GW, _wait

# The data path is a per-process temp dir (conftest.make_config); compute it
# the same way (the same pid the server fixture used).
DATA_PATH = f"/tmp/gpusybio-e2e-data-{os.getpid()}"
LOG_PATH = f"{DATA_PATH}/logs/a1.log"
PID_PATH = f"{DATA_PATH}/pids/a1.pid"


@pytest.fixture
def cfg_overrides():
    """Mark a1 as a llama app so the metrics handler uses the llama handler
    (which tails the app's log for the token lines)."""
    return {"application_types": {"a1": "llama"}}


async def test_llama_token_metrics_from_log(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    # 1. Start a1 (the start script redirects the app's stdout to the log and
    #    writes the pid to the data-path convention).
    r = await c.post(f"{admin}/api/applications/a1/start")
    assert r.status_code == 202, r.text

    async def a1_ready():
        d = (await c.get(f"{admin}/api/applications/a1")).json()
        return d["process_state"] == "RUNNING" and d["residency_state"] == "LOADED"

    await _wait(a1_ready, timeout=30)

    # The pid file and log land at the data-path convention.
    async def pid_exists():
        return os.path.exists(PID_PATH)

    await _wait(pid_exists, timeout=5)
    assert os.path.exists(PID_PATH)
    assert os.path.getsize(PID_PATH) > 0
    # the log exists (the app's stdout is redirected to it)
    async def log_exists():
        return os.path.exists(LOG_PATH)

    await _wait(log_exists, timeout=5)
    assert os.path.exists(LOG_PATH)

    # 2. Send a few requests through the gateway (the gateway records the
    #    per-request token counts from the upstream response headers).
    for _ in range(3):
        r = await c.post(
            f"http://127.0.0.1:{A1_GW}/generate",
            json={"chunks": 5, "input_tokens": 32},
            timeout=30.0,
        )
        assert r.status_code == 200

    # Give the streaming requests time to complete and flush logs (3 reqs × 5 chunks × 0.1s)
    await asyncio.sleep(2.0)

    # Each request logs a prompt + eval + total line to the app's stdout (the
    # log). Wait for the log to contain the token lines.
    async def log_has_tokens():
        try:
            with open(LOG_PATH) as f:
                text = f.read()
            return "prompt eval time" in text and "eval time" in text
        except OSError:
            return False

    await _wait(log_has_tokens, timeout=10)

    # 3. The metrics handler tails the log (1 s sampling); wait for the token
    #    metrics to appear (non-zero).
    async def token_metrics():
        m = (await c.get(f"{admin}/api/applications/a1/metrics")).json()
        raw = m.get("raw", {})
        return (raw.get("total_tokens") or 0) > 0 and (raw.get("input_tokens") or 0) > 0

    await _wait(token_metrics, timeout=8)
    m = (await c.get(f"{admin}/api/applications/a1/metrics")).json()
    raw = m["raw"]
    assert raw["input_tokens"] >= 96  # 3 requests x 32 input tokens
    assert raw["output_tokens"] >= 15  # 3 requests x 5 output tokens
    assert raw["total_tokens"] >= 111
    assert raw["requests"] >= 3
    assert raw["prompt_tokens_per_sec"] > 0
    assert raw["generation_tokens_per_sec"] > 0

    # 4. The request rows record the per-request token counts (from the headers).
    async def req_tokens():
        rows = (await c.get(
            f"{admin}/api/requests?status=COMPLETED")).json()
        # At least some completed requests should have token data captured
        return any(
            (r.get("input_tokens") or 0) > 0 or (r.get("output_tokens") or 0) > 0
            for r in rows
        )

    await _wait(req_tokens, timeout=10)
    await c.aclose()


async def test_pid_and_log_land_at_data_path(server):
    """The data-path convention: the pid and log land at the derived paths."""
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    r = await c.post(f"{admin}/api/applications/a2/start")
    assert r.status_code == 202, r.text

    async def a2_ready():
        d = (await c.get(f"{admin}/api/applications/a2")).json()
        return d["process_state"] == "RUNNING" and d["residency_state"] == "LOADED"

    await _wait(a2_ready, timeout=30)
    # the pid and log land at the data-path convention for a2
    assert os.path.exists(f"{DATA_PATH}/pids/a2.pid")
    assert os.path.getsize(f"{DATA_PATH}/pids/a2.pid") > 0
    assert os.path.exists(f"{DATA_PATH}/logs/a2.log")
    # the metrics API still returns the application's spec (auto-render)
    m = (await c.get(f"{admin}/api/applications/a2/metrics")).json()
    assert m["application_id"] == "a2"
    assert "metrics" in m
    await c.aclose()
