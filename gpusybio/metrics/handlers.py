"""The generic type handler (spec 04 §8.1): the fallback every application works
with even without a specialized type — process + GPU monitoring only.

Application-type handlers (llama, ollama, comfyui, tts) live in their own
modules under ``gpusybio/application_types/`` (spec 04 §8); this module holds
only the type-agnostic contract + the generic fallback.
"""

from __future__ import annotations

from typing import Protocol

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.events.log import get_logger

log = get_logger("handlers")


class ApplicationTypeHandler(Protocol):
    def collect_metrics(self, app_id: str) -> dict: ...  # noqa: A003
    def get_request_metrics(self, app_id: str, active: int = 0, queued: int = 0) -> dict: ...
    def detect_activity(self, app_id: str) -> bool: ...


class GenericHandler:
    """Fallback (spec 04 §8.1): process + GPU monitoring only."""

    def __init__(self, resources, states, request_metrics=None, base_urls=None,
                 session=None, log_paths=None) -> None:
        """Unified handler factory signature (spec 04 §8); unused args ignored."""
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
            st.process is ProcessState.STARTED
            and st.residency is ResidencyState.LOADED
            and self._resources.backend.process_utilization_pct(st.pid)
            > float(self._resources.gpu_activity_threshold_pct)
        )
