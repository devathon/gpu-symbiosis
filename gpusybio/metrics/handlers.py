"""Type handlers (spec 04 §8): application-specific metric collection + activity
detection, kept out of the scheduler.

The ``generic`` handler works for every application (process/GPU monitoring).
Specialized handlers (llama, ollama, comfyui, tts) add token/request metrics and
GPU-activity interpretation.
"""

from __future__ import annotations

import time
from typing import Protocol

import aiohttp

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.events.log import get_logger
from gpusybio.metrics.requests import RequestMetrics

log = get_logger("handlers")


class ApplicationTypeHandler(Protocol):
    def collect_metrics(self, app_id: str) -> dict: ...  # noqa: A003
    def get_request_metrics(self, app_id: str, active: int = 0, queued: int = 0) -> dict: ...
    def detect_activity(self, app_id: str) -> bool: ...


class GenericHandler:
    """Fallback (spec 04 §8.1): process + GPU monitoring only."""

    def __init__(self, resources, states) -> None:
        self._resources = resources
        self._states = states

    def collect_metrics(self, app_id: str) -> dict:
        st = self._states.get(app_id)
        return {
            "gpu_memory_mb": round(self._resources.observed_mb(app_id), 1),
            "gpu_utilization_pct": round(
                self._resources.backend.process_utilization_pct(st.pid), 1
            ),
            "process_state": st.process.value,
            "residency_state": st.residency.value,
        }

    def get_request_metrics(self, app_id: str, active: int = 0, queued: int = 0) -> dict:
        return {}

    def detect_activity(self, app_id: str) -> bool:
        st = self._states.get(app_id)
        return st.active_requests > 0 or (
            st.process is ProcessState.RUNNING
            and st.residency is ResidencyState.LOADED
            and self._resources.backend.process_utilization_pct(st.pid)
            > float(self._resources.gpu_activity_threshold_pct)
        )


class LlamaHandler:
    """Token metrics via the app's /stats endpoint (spec 08 §5)."""

    def __init__(self, resources, states, request_metrics: RequestMetrics,
                 base_urls: dict[str, str], session: aiohttp.ClientSession | None = None) -> None:
        self._resources = resources
        self._states = states
        self._rm = request_metrics
        self._base = base_urls  # app_id -> http://127.0.0.1:<port>
        self._session = session
        self._last: dict[str, tuple[dict, float]] = {}

    async def collect_metrics(self, app_id: str) -> dict:
        url = self._base.get(app_id)
        out: dict = {}
        if url and self._session is not None:
            try:
                async with self._session.get(f"{url}/stats", timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status == 200:
                        cur = await r.json()
                    else:
                        cur = {}
            except Exception:  # noqa: BLE001 - handler must never break sampling
                cur = {}
        else:
            cur = {}
        if cur:
            now = time.time()
            prev = self._last.get(app_id)
            self._last[app_id] = (cur, now)
            out = {
                "total_tokens": cur.get("total_tokens", cur.get("input_tokens", 0) + cur.get("output_tokens", 0)),
                "input_tokens": cur.get("input_tokens", 0),
                "output_tokens": cur.get("output_tokens", 0),
                "requests": cur.get("requests", 0),
                "completed_requests": cur.get("completed_requests", 0),
            }
            # Rates from deltas (spec 08 §5: tokens/sec, prompt/generation speed).
            if prev is not None:
                pt, pt_at = prev
                dt = max(1e-6, now - pt_at)
                d_out = max(0, cur.get("output_tokens", 0) - pt.get("output_tokens", 0))
                d_in = max(0, cur.get("input_tokens", 0) - pt.get("input_tokens", 0))
                out["tokens_per_sec"] = round((d_in + d_out) / dt, 2)
                out["generation_tokens_per_sec"] = round(d_out / dt, 2)
                out["prompt_tokens_per_sec"] = round(d_in / dt, 2)
        # Fall back to request-tracked token sums when /stats is absent.
        rm = self._rm.app(app_id)
        out.setdefault("total_tokens", rm["total_tokens"])
        out.setdefault("input_tokens", rm["input_tokens"])
        out.setdefault("output_tokens", rm["output_tokens"])
        return out

    def get_request_metrics(self, app_id: str, active: int = 0, queued: int = 0) -> dict:
        return self._rm.app(app_id, active, queued)

    def detect_activity(self, app_id: str) -> bool:
        st = self._states.get(app_id)
        if st.active_requests > 0:
            return True
        return (
            st.process is ProcessState.RUNNING
            and st.residency is ResidencyState.LOADED
            and self._resources.backend.process_utilization_pct(st.pid)
            > float(self._resources.gpu_activity_threshold_pct)
        )


class ComfyUIHandler:
    """Queue/progress API + GPU-activity interpretation (spec 06 §10)."""

    def __init__(self, resources, states, request_metrics: RequestMetrics,
                 base_urls: dict[str, str], session: aiohttp.ClientSession | None = None) -> None:
        self._resources = resources
        self._states = states
        self._rm = request_metrics
        self._base = base_urls
        self._session = session

    async def collect_metrics(self, app_id: str) -> dict:
        url = self._base.get(app_id)
        out: dict = {"is_active": "unknown", "queue_depth": None, "progress": None}
        if url and self._session is not None:
            try:
                async with self._session.get(
                    f"{url}/api/queue", timeout=aiohttp.ClientTimeout(total=2)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        items = data.get("queue_running", []) + data.get("queue_pending", [])
                        out["queue_depth"] = len(items)
                        if items:
                            out["progress"] = items[0].get("progress")
            except Exception:  # noqa: BLE001
                pass
        # GPU activity interpretation: busy GPU == ComfyUI generating.
        st = self._states.get(app_id)
        util = self._resources.backend.process_utilization_pct(st.pid)
        out["is_active"] = "true" if util > float(self._resources.gpu_activity_threshold_pct) else "false"
        return out

    def get_request_metrics(self, app_id: str, active: int = 0, queued: int = 0) -> dict:
        return self._rm.app(app_id, active, queued)

    def detect_activity(self, app_id: str) -> bool:
        st = self._states.get(app_id)
        return (
            st.process is ProcessState.RUNNING
            and st.residency is ResidencyState.LOADED
            and self._resources.backend.process_utilization_pct(st.pid)
            > float(self._resources.gpu_activity_threshold_pct)
        )


def make_handler(app_type: str, resources, states, request_metrics, base_urls, session=None):
    if app_type in ("llama", "ollama"):
        return LlamaHandler(resources, states, request_metrics, base_urls, session)
    if app_type == "comfyui":
        return ComfyUIHandler(resources, states, request_metrics, base_urls, session)
    return GenericHandler(resources, states)
