"""Persistence for lifetime metrics (spec 08 §5.1).

Reads/writes the per-application lifetime state so it survives restarts. The
type-specific fields are whatever the type's tailer ``state()`` returns
(keyed by the type's registered metric ids) — stored as a JSON document, NOT
columns, so a type's fields are dynamic (spec 04 §8): adding or renaming a
field never requires a schema change. The tailer's mechanism state (log byte
offset + session fingerprint) stays in its own columns. Written on a 60 s
timer for active apps and on app stop; seeded on server start.
"""

from __future__ import annotations

import json

from gpusybio.database.db import SessionFactory
from gpusybio.database.models import AppMetricLifetime
from gpusybio.events.log import get_logger

log = get_logger("metrics")

# Tailer mechanism state kept in its own columns (not display fields).
_MECHANISM = ("log_offset", "log_fingerprint")


def _parse_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        log.exception("lifetime document corrupt — starting a fresh lifetime")
        return {}


def _row_state(row: AppMetricLifetime) -> dict:
    state = _parse_json(row.lifetime)
    for f in _MECHANISM:
        state[f] = getattr(row, f)
    return state


class LifetimeStore:
    """app_id -> AppMetricLifetime row (upsert). Type-agnostic (spec 04 §8)."""

    def __init__(self, db: SessionFactory) -> None:
        self._db = db

    def load(self, app_id: str) -> dict | None:
        with self._db() as s:
            row = s.get(AppMetricLifetime, app_id)
            if row is None:
                return None
            return _row_state(row)

    def load_many(self, app_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        with self._db() as s:
            for app_id in app_ids:
                row = s.get(AppMetricLifetime, app_id)
                if row is not None:
                    out[app_id] = _row_state(row)
        return out

    def save(self, app_id: str, state: dict) -> None:
        with self._db() as s:
            row = s.get(AppMetricLifetime, app_id)
            if row is None:
                row = AppMetricLifetime(application_id=app_id)
                s.add(row)
            fields = {k: v for k, v in state.items() if k not in _MECHANISM}
            row.lifetime = json.dumps(fields)
            if "log_offset" in state:
                row.log_offset = int(state["log_offset"] or 0)
            if "log_fingerprint" in state:
                fp = state["log_fingerprint"]
                row.log_fingerprint = fp if isinstance(fp, bytes) else (
                    bytes(fp) if fp else None)
            s.commit()
