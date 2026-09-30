"""Pydantic configuration models (spec 03/05/10)."""

from __future__ import annotations

import os
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# Free-form (spec 04 §8): the type is resolved through the pluggable type
# registry — built-ins (llama, ollama, comfyui, tts), drop-in modules under
# <data_path>/plugins, "custom" (config-declared metrics), or the generic
# fallback for anything else.
AppType = str

WaitPolicy = Literal["REJECT", "EXPIRE", "ESCALATE_PRIORITY", "PROCESS_ANYWAY"]


class CustomMetricSpec(BaseModel):
    """One user-declared metric of the ``custom`` application type (spec 08 §9.6).

    The generic handler interprets the ``collect`` mapping (url + json_path) —
    no code required. Stored as a JSON document in the application row's
    dedicated ``metrics`` column, NOT in the human-readable ``description``.
    """

    id: str
    name: str
    kind: str = "gauge"  # gauge | counter | rate
    unit: str = ""
    source: str = "handler"  # handler | proxy | derived
    display: str = "card"  # card | chart
    group: str | None = None
    collect: dict = Field(default_factory=dict)  # handler fetch mapping
    aggregation: str | None = None  # avg | sum | max | min | last | percentile_95


class ApplicationSpec(BaseModel):
    """One managed GPU workload (spec 03)."""

    id: str
    name: str
    description: str | None = None
    process_name: str | None = None
    start_script: str
    stop_script: str | None = None
    load_script: str | None = None
    unload_script: str | None = None
    # Optional per-app log/pid paths (spec 03 §2.5). When unset, the id-based
    # defaults are used: <data_path>/logs/<id>.log and <data_path>/pids/<id>.pid.
    log_file: str | None = None
    pid_file: str | None = None

    port: int
    protocol: str = "http"
    health_check_url: str | None = None
    health_check_method: Literal["GET", "HEAD"] = "GET"
    health_check_timeout: float = 5.0
    ready_check_url: str | None = None

    application_type: AppType = "custom"
    # The ``custom`` type's declared metrics (spec 08 §9.6); ignored by other types.
    metrics: list[CustomMetricSpec] | None = None

    gpu_memory_mb: int = Field(ge=1)
    gpu_usage_pct: float | None = Field(default=None, ge=0, le=100)
    cpu_memory_mb: int | None = Field(default=None, ge=0)
    cpu_usage_pct: float | None = Field(default=None, ge=0, le=100)
    gpu_memory_safety_margin: float = Field(default=0.10, ge=0)

    allow_automatic_unload: bool = True
    idle_unload_timeout_sec: int | None = None
    activity_detection_method: str = "gpu_usage"
    idle_threshold_gpu_pct: float = 5.0
    # Memory-based residency monitor (spec 06 §10.2): LOADED while VRAM >= load
    # threshold, UNLOADED while < unload threshold (hysteresis).
    memory_load_threshold_pct: float = Field(default=80.0, ge=0, le=100)
    memory_unload_threshold_pct: float = Field(default=20.0, ge=0, le=100)
    pinned: bool = False

    api_enabled: bool = True
    supports_queueing: bool = True
    max_concurrent_requests: int = Field(default=1, ge=1)

    enabled: bool = True

    # Seed control (spec 16 §2.6): the seed creates missing rows only; an
    # existing DB row is updated at startup only if this is true.
    update: bool = False


class GatewaySpec(BaseModel):
    """One externally exposed port bound to an application (spec 05)."""

    id: str | None = None
    name: str
    port: int
    application_id: str
    priority: int = Field(default=5, ge=1)
    max_waiting_queue: int = Field(default=20, ge=1)
    max_waiting_time_sec: int = Field(default=300, ge=1)
    wait_timeout_policy: WaitPolicy = "ESCALATE_PRIORITY"
    request_timeout_sec: float | None = None
    max_concurrent_requests: int = Field(default=1, ge=1)
    enabled: bool = True

    # Seed control (spec 16 §2.6): the seed creates missing rows only; an
    # existing DB row is updated at startup only if this is true.
    update: bool = False


class GpuSpec(BaseModel):
    backend: Literal["auto", "nvml", "fake"] = "auto"
    fake_total_mb: int = 24 * 1024
    memory_reserve_mb: int = 2048
    memory_safety_margin: float = 0.10


class SchedulerSpec(BaseModel):
    interval_ms: int = 1000
    min_residency_sec: float = 30.0
    switch_margin: float = 5.0
    switch_cost_weight: float = 1.0
    emergency_wait_sec: float = 300.0
    emergency_queue_threshold: int | None = None
    switch_cooldown_sec: float = 0.0
    # Process Now (spec 06 §9): time-decaying priority boost until the
    # gateway's queue drains.
    process_now_multiplier: float = Field(default=2.0, ge=1.0)
    process_now_boost_ttl_sec: float = Field(default=120.0, ge=1.0)
    # Phase 4 (spec 06 §10): automatic idle detection + unloading.
    idle_unload_timeout_sec: float = Field(default=300.0, ge=1.0)
    idle_unload_interval_sec: float = Field(default=5.0, ge=0.5)


class MetricsSpec(BaseModel):
    interval_sec: float = 1.0


class QueueSpec(BaseModel):
    default_max_waiting_requests: int = 20
    default_max_waiting_time_sec: int = 300


class RequestsSpec(BaseModel):
    default_request_timeout_sec: float = 600.0


class LoadSpec(BaseModel):
    max_retries: int = 2
    retry_delay_sec: float = 10.0
    max_consecutive_failures: int = 3


class StartupSpec(BaseModel):
    max_detection_sec: float = 60.0
    # How long (in 1s steps) to wait for the start script to exit or to write
    # the pid file *fresh* (mtime at/after this start — a stale file from a
    # previous run is ignored) before falling back to the spawned script's PID
    # (spec 03 §2.5). Detached scripts exit in milliseconds; an attached script
    # (exec) hits this grace and resolves to proc.pid (which the system then
    # writes into the blank pid file), so keep it short.
    pid_grace_sec: float = 5.0


class SettingsSpec(BaseModel):
    log_level: str = "INFO"
    gpu: GpuSpec = GpuSpec()
    scheduler: SchedulerSpec = SchedulerSpec()
    metrics: MetricsSpec = MetricsSpec()
    queue: QueueSpec = QueueSpec()
    requests: RequestsSpec = RequestsSpec()
    load: LoadSpec = LoadSpec()
    startup: StartupSpec = StartupSpec()


class Config(BaseModel):
    db_path: str = "gpusybio.db"
    data_path: str | None = None
    admin_host: str = "127.0.0.1"
    admin_port: int = 9130
    settings: SettingsSpec = SettingsSpec()
    applications: list[ApplicationSpec] = []
    gateways: list[GatewaySpec] = []

    def effective_data_path(self) -> str:
        """The global data path: log at <data_path>/logs/<id>.log, pid at <data_path>/pids/<id>.pid."""
        if self.data_path:
            return os.path.expanduser(self.data_path)
        if os.path.isabs(self.db_path):
            base = os.path.basename(self.db_path).removesuffix(".db")
            return os.path.join(os.path.dirname(self.db_path), base or "gpusybio")
        return os.path.expanduser("~/.gpusybio")

    def app_log_path(self, app_id: str) -> str:
        return os.path.join(self.effective_data_path(), "logs", f"{app_id}.log")

    def app_pid_path(self, app_id: str) -> str:
        return os.path.join(self.effective_data_path(), "pids", f"{app_id}.pid")

    def effective_log_path(self, app_id: str, log_file: str | None = None) -> str:
        """The app's log path: the per-app ``log_file`` if set, else the
        id-based default (spec 03 §2.5)."""
        return os.path.expanduser(log_file) if log_file else self.app_log_path(app_id)

    def effective_pid_path(self, app_id: str, pid_file: str | None = None) -> str:
        """The app's pid path: the per-app ``pid_file`` if set, else the
        id-based default (spec 03 §2.5)."""
        return os.path.expanduser(pid_file) if pid_file else self.app_pid_path(app_id)


def load_yaml(path: str) -> Config:
    """Parse and validate a seed configuration file."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)
