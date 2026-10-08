"""The single ``active`` definition (spec 04 §3).

An application is **active** while it is *serving a request* OR *using the GPU*
(spec 04 §3) — if either signal is on, the app is active. This one definition is
consumed by:

- the **display** (drives the RUNNING display: STARTED + active),
- the **scheduler** (eviction guard: never evict an active app),
- the **idle-unloader** (never auto-unload an active app).
"""

from __future__ import annotations

from gpusybio.applications.lifecycle import AppState


def is_active(state: AppState, resources, threshold: float | None = None) -> bool:
    """True while the app is serving a request OR its GPU utilization is > the
    threshold. Either signal makes the app active.

    ``threshold`` is the per-app activity threshold when available, else the
    resource manager's global default (``gpu_activity_threshold_pct``).
    """
    if state.active_requests > 0:
        return True
    t = threshold if threshold is not None else resources.gpu_activity_threshold_pct
    return resources.app_utilization(state.app_id) > t
