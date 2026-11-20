"""YAML seed → database upsert (spec 16 §2.6: DB is source of truth)."""

from __future__ import annotations

import json

from gpusybio.config.models import Config
from gpusybio.database.db import SessionFactory
from gpusybio.database.models import Application, Gateway, Setting

SETTING_KEYS = {
    "gpu.memory_reserve_mb": lambda c: c.settings.gpu.memory_reserve_mb,
    "gpu.memory_safety_margin": lambda c: c.settings.gpu.memory_safety_margin,
    "gpu.backend": lambda c: c.settings.gpu.backend,
    "gpu.fake_total_mb": lambda c: c.settings.gpu.fake_total_mb,
    "scheduler.interval_ms": lambda c: c.settings.scheduler.interval_ms,
    "scheduler.min_residency_sec": lambda c: c.settings.scheduler.min_residency_sec,
    "scheduler.switch_margin": lambda c: c.settings.scheduler.switch_margin,
    "scheduler.switch_cost_weight": lambda c: c.settings.scheduler.switch_cost_weight,
    "scheduler.emergency_wait_sec": lambda c: c.settings.scheduler.emergency_wait_sec,
    "scheduler.emergency_queue_threshold": lambda c: c.settings.scheduler.emergency_queue_threshold,
    "scheduler.switch_cooldown_sec": lambda c: c.settings.scheduler.switch_cooldown_sec,
    "scheduler.process_now_multiplier": lambda c: c.settings.scheduler.process_now_multiplier,
    "scheduler.process_now_boost_ttl_sec": lambda c: c.settings.scheduler.process_now_boost_ttl_sec,
    "metrics.interval_sec": lambda c: c.settings.metrics.interval_sec,
    "queue.default_max_waiting_requests": lambda c: c.settings.queue.default_max_waiting_requests,
    "queue.default_max_waiting_time_sec": lambda c: c.settings.queue.default_max_waiting_time_sec,
    "requests.default_request_timeout_sec": lambda c: c.settings.requests.default_request_timeout_sec,
    "load.max_retries": lambda c: c.settings.load.max_retries,
    "load.retry_delay_sec": lambda c: c.settings.load.retry_delay_sec,
    "load.max_consecutive_failures": lambda c: c.settings.load.max_consecutive_failures,
    "startup.max_detection_sec": lambda c: c.settings.startup.max_detection_sec,
}


def _field_map(model) -> dict[str, str]:
    """Spec field name -> DB column name, read from the model (spec 09). A column
    whose name differs carries ``info={"spec_field": ...}`` in the model; every other
    column maps to itself. Derived here so the seed has no private copy that can go
    stale when a field is added under a different name.
    """
    return {c.info.get("spec_field", c.name): c.name for c in model.__table__.columns}


_APP_FIELD_MAP = _field_map(Application)
_GATEWAY_FIELD_MAP = _field_map(Gateway)
# Model fields stored as JSON documents in the DB. `metrics` is the same kind of
# column but its items are model objects, so it serializes through model_dump().
_JSON_FIELDS = {"command_env"}


def seed_database(factory: SessionFactory, config: Config) -> None:
    """Seed applications, gateways, and settings from the seed document.

    Seeding semantics (spec 16 §2.6 — DB is source of truth post-startup):
    the seed creates missing rows only. An existing application/gateway row is
    updated at startup ONLY if its spec sets ``update: true``; otherwise the
    row is left untouched so runtime changes (via ``PUT /api/*``) survive a
    restart. When the flag is set, only the fields the document declares are
    written — a field omitted from the YAML keeps the value stored in the DB,
    otherwise the model default would silently overwrite the runtime change.
    The seed never deletes rows absent from the document. Settings
    always overwrite (there is no settings API — YAML is the only way to
    change them). Existing rows keep their runtime-mutated fields (timing,
    observed memory, failure counts).
    """
    with factory() as s:
        for spec in config.applications:
            row = s.get(Application, spec.id)
            if row is None:
                row = Application(id=spec.id)
                s.add(row)
                _apply_app_config(row, spec, fresh=True)
            elif spec.update:
                _apply_app_config(row, spec)
            # existing row, no update flag — leave untouched

        for spec in config.gateways:
            gid = spec.id or f"gw-{spec.application_id}"
            row = s.get(Gateway, gid)
            if row is None:
                row = Gateway(id=gid)
                s.add(row)
                _apply_gateway_config(row, spec, gid, fresh=True)
            elif spec.update:
                _apply_gateway_config(row, spec, gid)
            # existing row, no update flag — leave untouched

        for key, getter in SETTING_KEYS.items():
            value = getter(config)
            if value is None:
                continue
            row = s.get(Setting, key)
            if row is None:
                row = Setting(key=key)
                s.add(row)
            row.value = json.dumps(value)


# Model fields that are seed control or row identity, not persisted columns.
_SKIP_FIELDS = {"id", "update"}


def _declared(spec, fresh: bool) -> set[str]:
    """Fields to write: the ones the YAML declares, or all of them for a row
    that is being created (its defaults are the intended initial values)."""
    fields = set(spec.model_fields) if fresh else spec.model_fields_set
    return fields - _SKIP_FIELDS


def _apply_app_config(row: Application, spec, fresh: bool = False) -> None:
    for name in _declared(spec, fresh):
        column = _APP_FIELD_MAP.get(name)
        if column is None:
            continue  # seed control or a column the seed must never write
        value = getattr(spec, name)
        if name == "metrics":
            value = json.dumps([m.model_dump() for m in value]) if value else None
        elif name in _JSON_FIELDS:
            value = json.dumps(value) if value else None
        setattr(row, column, value)

    # Derived: the app can be *unloaded* (freed while the process stays up) iff
    # it has an unload script. A load script is optional — if absent, an
    # unloaded app resumes serving without a load step.
    row.supports_load_unload = bool(row.unload_script)
    if row.observed_max_gpu_memory_mb is None:
        row.observed_max_gpu_memory_mb = spec.gpu_memory_mb
    if row.observed_max_gpu_usage_pct is None:
        row.observed_max_gpu_usage_pct = spec.gpu_usage_pct


def _apply_gateway_config(row: Gateway, spec, gid: str, fresh: bool = False) -> None:
    row.id = gid
    for name in _declared(spec, fresh):
        column = _GATEWAY_FIELD_MAP.get(name)
        if column is None:
            continue
        setattr(row, column, getattr(spec, name))
