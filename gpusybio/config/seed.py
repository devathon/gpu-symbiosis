"""YAML seed → database upsert (spec 16 §2.6: DB is source of truth)."""

from __future__ import annotations

import json
import uuid

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


def seed_database(factory: SessionFactory, config: Config) -> None:
    """Seed applications, gateways, and settings from the seed document.

    Seeding semantics (spec 16 §2.6 — DB is source of truth post-startup):
    the seed creates missing rows only. An existing application/gateway row is
    updated at startup ONLY if its spec sets ``update: true``; otherwise the
    row is left untouched so runtime changes (via ``PUT /api/*``) survive a
    restart. The seed never deletes rows absent from the document. Settings
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
            elif not spec.update:
                continue  # existing row, no update flag — leave untouched
            _apply_app_config(row, spec)

        for spec in config.gateways:
            gid = spec.id or f"gw-{spec.application_id}"
            row = s.get(Gateway, gid)
            if row is None:
                row = Gateway(id=gid)
                s.add(row)
            elif not spec.update:
                continue  # existing row, no update flag — leave untouched
            _apply_gateway_config(row, spec, gid)

        for key, getter in SETTING_KEYS.items():
            value = getter(config)
            if value is None:
                continue
            row = s.get(Setting, key)
            if row is None:
                row = Setting(key=key)
                s.add(row)
            row.value = json.dumps(value)


def _apply_app_config(row: Application, spec) -> None:
    row.name = spec.name
    row.description = spec.description
    row.process_name = spec.process_name
    row.start_script = spec.start_script
    row.stop_script = spec.stop_script
    row.load_script = spec.load_script
    row.unload_script = spec.unload_script
    row.port = spec.port
    row.protocol = spec.protocol
    row.health_check_url = spec.health_check_url
    row.health_check_method = spec.health_check_method
    row.health_check_timeout = spec.health_check_timeout
    row.ready_check_url = spec.ready_check_url
    row.application_type = spec.application_type
    row.configured_gpu_memory_mb = spec.gpu_memory_mb
    row.configured_gpu_usage_pct = spec.gpu_usage_pct
    row.cpu_memory_required_mb = spec.cpu_memory_mb
    row.cpu_usage_expected_pct = spec.cpu_usage_pct
    row.gpu_memory_safety_margin = spec.gpu_memory_safety_margin
    # Derived: the app can be *unloaded* (freed while the process stays up) iff
    # it has an unload script. A load script is optional — if absent, an
    # unloaded app resumes serving without a load step.
    row.supports_load_unload = bool(spec.unload_script)
    row.allow_automatic_unload = spec.allow_automatic_unload
    row.idle_unload_timeout_sec = spec.idle_unload_timeout_sec
    row.activity_detection_method = spec.activity_detection_method
    row.idle_threshold_gpu_pct = spec.idle_threshold_gpu_pct
    row.pinned = spec.pinned
    row.api_enabled = spec.api_enabled
    row.supports_queueing = spec.supports_queueing
    row.max_concurrent_requests = spec.max_concurrent_requests
    row.enabled = spec.enabled
    if row.observed_max_gpu_memory_mb is None:
        row.observed_max_gpu_memory_mb = spec.gpu_memory_mb
    if row.observed_max_gpu_usage_pct is None:
        row.observed_max_gpu_usage_pct = spec.gpu_usage_pct


def _apply_gateway_config(row: Gateway, spec, gid: str) -> None:
    row.id = gid
    row.name = spec.name
    row.port = spec.port
    row.application_id = spec.application_id
    row.priority = spec.priority
    row.max_waiting_queue = spec.max_waiting_queue
    row.max_waiting_time_sec = spec.max_waiting_time_sec
    row.wait_timeout_policy = spec.wait_timeout_policy
    row.request_timeout_sec = spec.request_timeout_sec
    row.max_concurrent_requests = spec.max_concurrent_requests
    row.enabled = spec.enabled


def new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"
