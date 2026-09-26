"""ORM models (spec 09)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    process_name: Mapped[str | None] = mapped_column(Text)

    start_script: Mapped[str] = mapped_column(Text)
    stop_script: Mapped[str | None] = mapped_column(Text)
    load_script: Mapped[str | None] = mapped_column(Text)
    unload_script: Mapped[str | None] = mapped_column(Text)

    port: Mapped[int] = mapped_column(Integer)
    protocol: Mapped[str] = mapped_column(Text, default="http")
    health_check_url: Mapped[str | None] = mapped_column(Text)
    health_check_method: Mapped[str] = mapped_column(Text, default="GET")
    health_check_timeout: Mapped[float] = mapped_column(Float, default=5.0)
    ready_check_url: Mapped[str | None] = mapped_column(Text)

    application_type: Mapped[str] = mapped_column(Text, default="custom")

    configured_gpu_memory_mb: Mapped[int] = mapped_column(Integer)
    observed_max_gpu_memory_mb: Mapped[int | None] = mapped_column(Integer)
    configured_gpu_usage_pct: Mapped[float | None] = mapped_column(Float)
    observed_max_gpu_usage_pct: Mapped[float | None] = mapped_column(Float)
    cpu_memory_required_mb: Mapped[int | None] = mapped_column(Integer)
    cpu_usage_expected_pct: Mapped[float | None] = mapped_column(Float)
    gpu_memory_safety_margin: Mapped[float] = mapped_column(Float, default=0.10)

    startup_time_sec: Mapped[float | None] = mapped_column(Float)
    load_time_sec: Mapped[float | None] = mapped_column(Float)
    unload_time_sec: Mapped[float | None] = mapped_column(Float)
    stop_time_sec: Mapped[float | None] = mapped_column(Float)

    supports_load_unload: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_automatic_unload: Mapped[bool] = mapped_column(Boolean, default=True)
    idle_unload_timeout_sec: Mapped[int | None] = mapped_column(Integer)
    activity_detection_method: Mapped[str] = mapped_column(Text, default="gpu_usage")
    idle_threshold_gpu_pct: Mapped[float] = mapped_column(Float, default=5.0)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    api_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_queueing: Mapped[bool] = mapped_column(Boolean, default=True)
    max_concurrent_requests: Mapped[int] = mapped_column(Integer, default=1)

    consecutive_load_failures: Mapped[int] = mapped_column(Integer, default=0)
    faulted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def effective_gpu_memory_mb(self) -> int:
        obs = self.observed_max_gpu_memory_mb
        if obs is None:
            return self.configured_gpu_memory_mb
        return max(self.configured_gpu_memory_mb, obs)


class Gateway(Base):
    __tablename__ = "gateways"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    port: Mapped[int] = mapped_column(Integer, unique=True)
    application_id: Mapped[str] = mapped_column(
        Text, ForeignKey("applications.id"), nullable=False
    )

    priority: Mapped[int] = mapped_column(Integer, default=5)
    max_waiting_queue: Mapped[int] = mapped_column(Integer, default=20)
    max_waiting_time_sec: Mapped[int] = mapped_column(Integer, default=300)
    wait_timeout_policy: Mapped[str] = mapped_column(Text, default="ESCALATE_PRIORITY")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    request_timeout_sec: Mapped[float | None] = mapped_column(Float)
    max_concurrent_requests: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class GatewayApiKey(Base):
    """Per-gateway API key (spec 12 §2). Only the SHA-256 hash is stored; the
    plaintext key is shown once at creation."""

    __tablename__ = "gateway_api_keys"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    gateway_id: Mapped[str] = mapped_column(Text, ForeignKey("gateways.id"), nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)  # sha256 hex
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminUser(Base):
    """Local admin account (spec 12 §3). Password stored salted + hashed
    (scrypt); only the hash is persisted, never the plaintext."""

    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)  # scrypt$<salt>$<hash>
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Request(Base):
    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_gateway_status", "gateway_id", "status"),
        Index("ix_requests_app_status", "application_id", "status"),
        Index("ix_requests_queued_at", "queued_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    gateway_id: Mapped[str] = mapped_column(Text, ForeignKey("gateways.id"), nullable=False)
    application_id: Mapped[str] = mapped_column(
        Text, ForeignKey("applications.id"), nullable=False
    )

    status: Mapped[str] = mapped_column(Text, default="QUEUED")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    priority: Mapped[int] = mapped_column(Integer, default=5)
    client_metadata: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    queue_wait_sec: Mapped[float | None] = mapped_column(Float)
    service_time_sec: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)


class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GatewayEvent(Base):
    __tablename__ = "gateway_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gateway_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SchedulerEvent(Base):
    __tablename__ = "scheduler_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(Text)
    candidate_gateway_id: Mapped[str | None] = mapped_column(Text)
    candidate_application_id: Mapped[str | None] = mapped_column(Text)
    candidate_score: Mapped[float | None] = mapped_column(Float)
    switching_penalty: Mapped[float | None] = mapped_column(Float)
    effective_score: Mapped[float | None] = mapped_column(Float)
    current_score: Mapped[float | None] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricSample(Base):
    """1 s samples (metrics_raw, spec 08 §6)."""

    __tablename__ = "metrics_raw"
    __table_args__ = (Index("ix_metrics_app_at", "application_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str | None] = mapped_column(Text)  # None = system-wide GPU
    gpu_pct: Mapped[float | None] = mapped_column(Float)
    gpu_mem_mb: Mapped[float | None] = mapped_column(Float)
    gpu_temp_c: Mapped[float | None] = mapped_column(Float)
    gpu_power_w: Mapped[float | None] = mapped_column(Float)
    cpu_pct: Mapped[float | None] = mapped_column(Float)
    cpu_rss_mb: Mapped[float | None] = mapped_column(Float)
    threads: Mapped[int | None] = mapped_column(Integer)
    active_requests: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricAggregate(Base):
    """Rolled-up metric windows (spec 08 §6): 1-minute (30 d) and hourly
    (indefinite). Each row is one (scope, metric, window) aggregate."""

    __tablename__ = "metrics_aggregate"
    __table_args__ = (Index("ix_metrics_agg_scope_at", "scope", "metric", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(Text)  # "gpu" | app_id
    metric: Mapped[str] = mapped_column(Text)  # e.g. gpu_mem_mb, active_requests
    window_sec: Mapped[int] = mapped_column(Integer)  # 60 (1-min) or 3600 (hourly)
    min: Mapped[float | None] = mapped_column(Float)
    max: Mapped[float | None] = mapped_column(Float)
    avg: Mapped[float | None] = mapped_column(Float)
    total: Mapped[float | None] = mapped_column(Float)
    count: Mapped[int] = mapped_column(Integer, default=1)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON scalar
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
