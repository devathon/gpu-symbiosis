"""State Manager (spec 02 §2): owns application state machines.

Enforces the invariants at transition time:
- Invariant 1: no unload with active requests.
- Invariant 3: one lifecycle transition per application.
- Invariant 7: manual actions drain first (force is the only exception).
"""

from __future__ import annotations

from gpusybio.applications.lifecycle import (
    AppState,
    ProcessState,
    ResidencyState,
    is_valid_combination,
)


class InvalidTransition(Exception):
    pass


class StateManager:
    def __init__(self) -> None:
        self._states: dict[str, AppState] = {}

    def ensure(self, app_id: str) -> AppState:
        if app_id not in self._states:
            self._states[app_id] = AppState(app_id=app_id)
        return self._states[app_id]

    def get(self, app_id: str) -> AppState:
        return self.ensure(app_id)

    def all(self) -> dict[str, AppState]:
        return dict(self._states)

    # -- invariant-checked transitions --------------------------------------
    def begin_transition(self, app_id: str, name: str) -> AppState:
        s = self.get(app_id)
        if s.transition is not None:
            raise InvalidTransition(
                f"{app_id}: transition '{s.transition}' already in flight (Invariant 3)"
            )
        s.set_transition(name)
        return s

    def finish_transition(self, app_id: str) -> AppState:
        s = self.get(app_id)
        s.clear_transition()
        s.error = None
        _assert_valid(app_id, s)
        return s

    def set_process(self, app_id: str, state: ProcessState) -> AppState:
        s = self.get(app_id)
        s.process = state
        _assert_valid(app_id, s)
        return s

    def set_residency(self, app_id: str, state: ResidencyState) -> AppState:
        s = self.get(app_id)
        s.residency = state
        _assert_valid(app_id, s)
        return s

    def set_pid(self, app_id: str, pid: int | None) -> None:
        self.get(app_id).pid = pid

    # -- invariant 1 ---------------------------------------------------------
    def can_unload(self, app_id: str) -> tuple[bool, str]:
        s = self.get(app_id)
        if s.residency is not ResidencyState.LOADED:
            return False, f"residency is {s.residency.value}, not LOADED"
        if s.active_requests > 0:
            return False, f"active_requests={s.active_requests} (Invariant 1: drain first)"
        if s.transition is not None:
            return False, f"transition '{s.transition}' in flight (Invariant 3)"
        return True, "ok"

    # -- request tracking ------------------------------------------------------
    def request_started(self, app_id: str) -> None:
        self.get(app_id).active_requests += 1

    def request_finished(self, app_id: str) -> None:
        s = self.get(app_id)
        s.active_requests = max(0, s.active_requests - 1)

    def set_draining(self, app_id: str, draining: bool) -> None:
        self.get(app_id).draining = draining


def _assert_valid(app_id: str, s: AppState) -> None:
    if not is_valid_combination(s.process, s.residency):
        raise InvalidTransition(
            f"{app_id}: invalid combination {s.process.value}/{s.residency.value}"
        )
