"""spec 11 §5 — the scheduler's VRAM accounting must never trigger a blocking
NVML ``sample()`` while holding the scheduler lock.

``_vram_accounting(allow_sample=False)`` (used by ``_decide`` under the lock)
reads cached state only; with no cached sample it reports a degraded zeroed
accounting instead of calling the backend. The API current-decision endpoint
keeps the default ``allow_sample=True``.
"""

from __future__ import annotations

from gpusybio.scheduler import Scheduler


class _NoSampleResources:
    """Stub resource manager: a cached sample is absent; sample() must never run."""

    last_sample = None

    def sample(self):
        raise AssertionError("sample() must not be called with allow_sample=False")

    def observed_mb(self, app_id):
        return 0.0

    def reservations(self):
        return {}


class _Gpu:
    memory_reserve_mb = 1024.0


class _Settings:
    gpu = _Gpu()


def test_vram_accounting_never_samples_under_lock():
    sched = Scheduler(
        states=None,
        resources=_NoSampleResources(),
        request_manager=None,
        apps=None,
        settings=_Settings(),
        db=None,
        bus=None,
        app_ids=["a1", "a2"],
        gateway_ids=["gw-a1"],
    )
    # Under the lock (allow_sample=False): no sample, degraded zeroed accounting.
    acc = sched._vram_accounting(allow_sample=False)
    assert acc["total_mb"] == 0.0
    assert acc["stale"] is True
    assert acc["reserve_mb"] == 1024.0
    assert acc["resident_mb"] == 0.0


def test_vram_accounting_reports_cached_sample():
    class _Sample:
        total_mb = 24576.0
        used_mb = 10000.0

    class _Resources(_NoSampleResources):
        last_sample = _Sample()

    sched = Scheduler(
        states=None,
        resources=_Resources(),
        request_manager=None,
        apps=None,
        settings=_Settings(),
        db=None,
        bus=None,
        app_ids=["a1"],
        gateway_ids=["gw-a1"],
    )
    # A cached sample is reported (no sample() call needed at all).
    acc = sched._vram_accounting(allow_sample=False)
    assert acc["total_mb"] == 24576.0
    assert acc["usable_mb"] == round(24576.0 - 1024.0, 1)
    assert "stale" not in acc
