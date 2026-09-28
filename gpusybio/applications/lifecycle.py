"""Two independent state machines (spec 04 §2) + valid combinations (§2.3)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProcessState(Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class ResidencyState(Enum):
    OFF = "OFF"
    LOADING = "LOADING"
    LOADED = "LOADED"
    UNLOADING = "UNLOADING"


# spec 04 §2.3 — the only valid (process, residency) combinations.
_VALID = {
    (ProcessState.STOPPED, ResidencyState.OFF),
    (ProcessState.STARTING, ResidencyState.OFF),
    (ProcessState.STARTING, ResidencyState.LOADING),
    (ProcessState.RUNNING, ResidencyState.OFF),
    (ProcessState.RUNNING, ResidencyState.LOADING),
    (ProcessState.RUNNING, ResidencyState.LOADED),
    (ProcessState.RUNNING, ResidencyState.UNLOADING),
    (ProcessState.STOPPING, ResidencyState.OFF),
    (ProcessState.ERROR, ResidencyState.OFF),
    (ProcessState.ERROR, ResidencyState.LOADING),
    (ProcessState.ERROR, ResidencyState.LOADED),
    (ProcessState.ERROR, ResidencyState.UNLOADING),
}


def is_valid_combination(process: ProcessState, residency: ResidencyState) -> bool:
    return (process, residency) in _VALID


@dataclass
class AppState:
    """Runtime state for one application (owned by the State Manager)."""

    app_id: str
    process: ProcessState = ProcessState.STOPPED
    residency: ResidencyState = ResidencyState.OFF
    pid: int | None = None
    draining: bool = False
    error: str | None = None
    active_requests: int = 0
    queued_requests: int = 0
    loaded_at: float | None = None
    # In-flight transition (Invariant 3): one at a time, never overlapping.
    transition: str | None = None  # "start" | "stop" | "load" | "unload"

    def set_transition(self, name: str) -> None:
        # Invariant 3: a second transition request while one is in flight is
        # rejected by the caller; the state machine records only one.
        assert self.transition is None, (
            f"{self.app_id}: transition '{self.transition}' already in flight, "
            f"refusing '{name}'"
        )
        self.transition = name

    def clear_transition(self) -> None:
        self.transition = None


def combined_ui_state(state: AppState, active: bool = False) -> str:
    """Simplified projection for the UI (spec 04 §3): one of ``STOPPED``,
    ``IDLE``, ``IDLE & LOADED``, ``RUNNING`` (plus the transient states).

    ``active`` (spec 04 §3) is serving a request OR using the GPU — it drives
    the ``RUNNING`` state. LOADED is based on VRAM (residency); RUNNING is based
    on GPU utilization (``active``); STARTED (up, model not loaded) shows as
    ``IDLE``. Apps without load/unload keep residency OFF, so they show only
    ``STOPPED`` / ``IDLE`` / ``RUNNING``.
    """
    p, r = state.process, state.residency
    if p is ProcessState.ERROR:
        return "ERROR"
    if p is ProcessState.STOPPING:
        return "STOPPING"
    if p is ProcessState.STARTING:
        return "STARTING"
    if p is ProcessState.STOPPED:
        return "STOPPED"
    if r is ResidencyState.LOADING:
        return "LOADING"
    if r is ResidencyState.UNLOADING:
        return "UNLOADING"
    if r is ResidencyState.LOADED and state.draining:
        return "DRAINING"
    if active:
        return "RUNNING"
    if r is ResidencyState.LOADED:
        return "IDLE & LOADED"
    return "IDLE"
