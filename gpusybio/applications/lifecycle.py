"""Two independent state machines (spec 04 §2) + valid combinations (§2.3)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProcessState(Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    # "Started" = the process is up. Whether it is *active* (serving / using the
    # GPU) is a separate concern — the UI shows STARTED as RUNNING when active.
    STARTED = "STARTED"
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
    (ProcessState.STARTED, ResidencyState.OFF),
    (ProcessState.STARTED, ResidencyState.LOADING),
    (ProcessState.STARTED, ResidencyState.LOADED),
    (ProcessState.STARTED, ResidencyState.UNLOADING),
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
    drain_failures: int = 0
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
