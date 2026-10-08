"""Switch-in verification (Invariant 1, spec 07 §8) + the drain guard.

``_do_switch`` must NOT load the candidate when the victim's VRAM was not
actually freed (a failed unload leaves the victim resident). A draining victim
that is still using the GPU (rolling-average ``is_active``) is not switched out.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.applications.manager import AppConfig
from gpusybio.events.bus import EventBus
from gpusybio.scheduler import Scheduler


@dataclass
class _VS:
    residency: ResidencyState
    process: ProcessState = ProcessState.STARTED
    draining: bool = False
    drain_failures: int = 0
    active_requests: int = 0
    app_id: str = ""


class _States:
    def __init__(self, a1: _VS):
        self._m = {"a1": a1, "a2": _VS(ResidencyState.OFF, app_id="a2")}

    def get(self, app_id: str) -> _VS:
        return self._m[app_id]

    def set_draining(self, app_id: str, draining: bool) -> None:
        self._m[app_id].draining = draining


class _Apps:
    async def release(self, app_id: str):
        # Stub: the state (resident or freed) is set by the test via _States.
        pass

    def config(self, app_id: str) -> AppConfig:
        return _cfg(app_id)


class _RM:
    def app_limit(self, app_id: str) -> int:
        return 4


class _Settings:
    class scheduler:
        process_now_boost_ttl_sec = 30.0
        interval_ms = 500


def _cfg(app_id: str) -> AppConfig:
    return AppConfig(
        id=app_id, name=app_id, start_script="x", stop_script=None, load_script=None,
        unload_script="u", port=1, health_check_url=None, health_check_method="GET",
        health_check_timeout=1.0, ready_check_url=None, configured_gpu_memory_mb=4096,
        effective_gpu_memory_mb=4096, loaded_gpu_memory_mb=0.0, safety_margin=0.1, supports_load_unload=True,
        allow_automatic_unload=True, pinned=False, max_concurrent_requests=4,
        enabled=True, consecutive_load_failures=0, faulted_until=None,
        process_name=None,
    )


def _sched(victim: _VS, loaded_candidate: bool = True):
    states = _States(victim)
    sched = Scheduler(
        states=states, resources=None, request_manager=_RM(), apps=_Apps(),
        settings=_Settings(), db=None, bus=EventBus(),
        app_ids=["a1", "a2"], gateway_ids=[],
    )
    loaded: list[str] = []
    emitted: list[tuple[str, dict]] = []

    async def _ensure_running_loaded(app_id: str) -> _VS:
        loaded.append(app_id)
        return _VS(ResidencyState.LOADED, app_id=app_id) if loaded_candidate else _VS(ResidencyState.OFF, app_id=app_id)

    def _forward(app_id: str, limit: int) -> int:
        return 0

    async def _emit(event_type: str, **data) -> None:
        emitted.append((event_type, data))

    sched._ensure_running_loaded = _ensure_running_loaded
    sched._forward = _forward
    sched._emit = _emit
    return sched, loaded, emitted


def test_do_switch_aborts_when_victim_vram_not_freed():
    """Invariant 1: a failed unload leaves the victim resident (LOADED); the
    candidate must NOT be loaded on top (it would OOM)."""
    sched, loaded, emitted = _sched(
        _VS(ResidencyState.LOADED, draining=True, app_id="a1")
    )
    ok = asyncio.run(sched._do_switch(_cfg("a1"), "a2", "test"))
    assert ok is False
    assert "a2" not in loaded  # the candidate was NOT loaded
    assert any(e[0] == "SWITCH_ABORTED" for e in emitted)


def test_do_switch_proceeds_when_victim_released():
    """When the victim's VRAM is actually freed (residency OFF), the candidate
    is loaded and the switch succeeds."""
    sched, loaded, emitted = _sched(
        _VS(ResidencyState.OFF, app_id="a1")
    )
    ok = asyncio.run(sched._do_switch(_cfg("a1"), "a2", "test"))
    assert ok is True
    assert "a2" in loaded  # the candidate WAS loaded
    assert any(e[0] == "SWITCH_IN" for e in emitted)


def test_do_switch_aborts_and_restores_draining():
    """The drain-completion path clears the victim's ``draining`` flag *before*
    the Invariant-1 check. When the switch then aborts (victim VRAM not freed),
    the flag must be RESTORED — otherwise the next evaluation's resident-first
    path forwards the victim's queued request and the candidate path loads on
    top (the comfyui→qwen3827b-lg race)."""
    sched, loaded, emitted = _sched(
        _VS(ResidencyState.LOADED, draining=True, app_id="a1")
    )
    ok = asyncio.run(sched._do_switch(_cfg("a1"), "a2", "test"))
    assert ok is False
    assert "a2" not in loaded  # the candidate was NOT loaded on top
    # The fix: the draining flag is restored after the abort, so the victim is
    # still skipped by the resident-first path and the candidate is not loaded.
    assert sched._states.get("a1").draining is True


def test_do_switch_gives_up_after_3_drain_failures():
    """After 3 consecutive drain failures, the scheduler clears the draining
    flag so the drain-completion check stops re-triggering the unload (the
    comfyui→UNLOADING→UNLOAD_FAILED infinite loop)."""
    victim = _VS(ResidencyState.LOADED, draining=True, drain_failures=2, app_id="a1")
    sched, loaded, emitted = _sched(victim)
    ok = asyncio.run(sched._do_switch(_cfg("a1"), "a2", "test"))
    assert ok is False
    assert "a2" not in loaded
    # After the 3rd failure, the draining flag is cleared (give up).
    assert sched._states.get("a1").draining is False
    assert sched._states.get("a1").drain_failures == 0  # reset for next drain cycle


def test_do_switch_resets_drain_failures_on_success():
    """When the switch succeeds (victim VRAM freed), the drain_failures counter
    is reset so the next drain cycle starts fresh."""
    victim = _VS(ResidencyState.OFF, drain_failures=2, app_id="a1")
    sched, loaded, emitted = _sched(victim)
    ok = asyncio.run(sched._do_switch(_cfg("a1"), "a2", "test"))
    assert ok is True
    assert "a2" in loaded
    assert sched._states.get("a1").drain_failures == 0  # reset on success
