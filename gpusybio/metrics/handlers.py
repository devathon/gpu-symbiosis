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
from gpusybio.metrics.llama_log import LlamaLogMetrics
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
    """Token metrics from the app's log (spec 08 §5); /stats + request-tracked
    as fallbacks.

    A real ``llama-server`` exposes no cumulative token totals over HTTP, but it
    logs each request. The app's stdout is the log (``<data_path>/logs/<id>.log``),
    so the handler tails it. Apps that do expose ``/stats`` (e.g. the mock) still
    work: the first non-zero source wins (log, then /stats, then request-tracked).
    """

    _TOKEN_KEYS = (
        "total_tokens", "input_tokens", "output_tokens", "requests", "completed_requests",
        "tokens_per_sec", "prompt_tokens_per_sec", "generation_tokens_per_sec",
    )

    def __init__(self, resources, states, request_metrics: RequestMetrics,
                 base_urls: dict[str, str], session: aiohttp.ClientSession | None = None,
                 log_paths: dict[str, str] | None = None) -> None:
        self._resources = resources
        self._states = states
        self._rm = request_metrics
        self._base = base_urls  # app_id -> http://127.0.0.1:<port>
        self._session = session
        self._last: dict[str, tuple[dict, float]] = {}
        # app_id -> tailer of its log (the app's stdout)
        self._tailers = {aid: LlamaLogMetrics(path) for aid, path in (log_paths or {}).items()}

    def _log_metrics(self, app_id: str) -> dict:
        t = self._tailers.get(app_id)
        return t.read() if t is not None else {}

    async def _stats(self, app_id: str) -> dict:
        url = self._base.get(app_id)
        if not url or self._session is None:
            return {}
        try:
            async with self._session.get(f"{url}/stats", timeout=aiohttp.ClientTimeout(total=2)) as r:
                cur = await r.json() if r.status == 200 else {}
        except Exception:  # noqa: BLE001 - handler must never break sampling
            cur = {}
        if not cur:
            return {}
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
        return out

    async def collect_metrics(self, app_id: str) -> dict:
        # Log-derived (primary for real llama-server), then /stats, then request-tracked.
        sources = (
            self._log_metrics(app_id),
            await self._stats(app_id),
            {
                "total_tokens": self._rm.app(app_id).get("total_tokens", 0),
                "input_tokens": self._rm.app(app_id).get("input_tokens", 0),
                "output_tokens": self._rm.app(app_id).get("output_tokens", 0),
            },
        )
        out: dict = {}
        for key in self._TOKEN_KEYS:
            for src in sources:
                v = src.get(key)
                if v:
                    out[key] = v
                    break
            else:
                out[key] = 0
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


def make_handler(app_type: str, resources, states, request_metrics, base_urls, session=None,
                 log_paths: dict[str, str] | None = None):
    if app_type in ("llama", "ollama"):
        return LlamaHandler(resources, states, request_metrics, base_urls, session, log_paths=log_paths)
    if app_type == "comfyui":
        return ComfyUIHandler(resources, states, request_metrics, base_urls, session)
    return GenericHandler(resources, states)
