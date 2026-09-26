"""Pydantic configuration models (spec 03/05/10)."""

from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, Field

AppType = Literal[
    "llama",
    "ollama",
    "harness",
    "comfyui",
    "tts",
    "image_generation",
    "video_generation",
    "embedding",
    "custom",
]

WaitPolicy = Literal["REJECT", "EXPIRE", "ESCALATE_PRIORITY", "PROCESS_ANYWAY"]


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

    port: int
    protocol: str = "http"
    health_check_url: str | None = None
    health_check_method: Literal["GET", "HEAD"] = "GET"
    health_check_timeout: float = 5.0
    ready_check_url: str | None = None

    application_type: AppType = "custom"

    gpu_memory_mb: int = Field(ge=1)
    gpu_usage_pct: float | None = Field(default=None, ge=0, le=100)
    cpu_memory_mb: int | None = Field(default=None, ge=0)
    cpu_usage_pct: float | None = Field(default=None, ge=0, le=100)
    gpu_memory_safety_margin: float = Field(default=0.10, ge=0)

    allow_automatic_unload: bool = True
    idle_unload_timeout_sec: int | None = None
    activity_detection_method: str = "gpu_usage"
    idle_threshold_gpu_pct: float = 5.0
    pinned: bool = False

    api_enabled: bool = True
    supports_queueing: bool = True
    max_concurrent_requests: int = Field(default=1, ge=1)

    enabled: bool = True


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
    admin_host: str = "127.0.0.1"
    admin_port: int = 9130
    settings: SettingsSpec = SettingsSpec()
    applications: list[ApplicationSpec] = []
    gateways: list[GatewaySpec] = []


def load_yaml(path: str) -> Config:
    """Parse and validate a seed configuration file."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)
