"""The ``comfyui`` application type (spec 04 §8, 06 §10): queue/progress API
metrics + GPU-activity interpretation (a busy GPU means ComfyUI is generating).
"""

from __future__ import annotations

import aiohttp

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.metrics.requests import RequestMetrics
from gpusybio.metrics.type_registry import TypePlugin
from gpusybio.metrics.typespec import MetricDef, TypeSpec


# spec 05 §6: only the prompt-submission endpoint goes through the scheduler;
# reads / static / websocket pass through directly. The set is a class
# attribute so it can be extended without subclassing again.
class ComfyUISpec(TypeSpec):
    scheduled_endpoints: set[tuple[str, str]] = {("POST", "/api/prompt")}

    def should_schedule(self, method: str, path: str) -> bool:
        return (method.upper(), path) in self.scheduled_endpoints


COMFYUI_SPEC = ComfyUISpec("comfyui", [
    MetricDef("queue_depth", "Queue depth", "gauge", "", "handler", "card"),
    MetricDef("progress", "Progress", "gauge", "", "handler", "card"),
    MetricDef("is_active", "Active", "text", "", "handler", "card"),
    MetricDef("idle_sec", "Idle", "gauge", "", "sample", "card"),
])


class ComfyUIHandler:
    """Queue/progress API + GPU-activity interpretation (spec 06 §10)."""

    def __init__(self, resources, states, request_metrics: RequestMetrics,
                 base_urls: dict[str, str], session: aiohttp.ClientSession | None = None,
                 log_paths=None) -> None:
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


def register(registry) -> None:
    registry.register(TypePlugin("comfyui", COMFYUI_SPEC, ComfyUIHandler))
