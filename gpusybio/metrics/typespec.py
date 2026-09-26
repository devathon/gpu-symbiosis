"""Type metrics contract (spec 08 §9): what is recorded/displayed is declared by
a TypeSpec; how it is fetched is the TypeHandler's job.

MetricDef is a pure dataclass — the UI auto-renders from it. Built-in specs
(llama, ollama, comfyui, tts) are registered; ``custom`` reads its metric list
from the application's config; ``generic`` is the fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MetricDef:
    id: str
    name: str
    kind: str = "gauge"  # gauge | rate | counter | histogram | text
    unit: str = ""
    source: str = "sample"  # sample | handler | config | computed
    display: str = "card"  # card | chart | table | text
    collect: dict = field(default_factory=dict)  # handler fetch mapping
    aggregation: str = "avg"  # avg | sum | max | min | last | percentile_95


@dataclass
class TypeSpec:
    type: str
    metrics: list[MetricDef] = field(default_factory=list)

    def metric(self, metric_id: str) -> MetricDef | None:
        for m in self.metrics:
            if m.id == metric_id:
                return m
        return None


# -- built-in specs (spec 08 §5/§9) -------------------------------------------

LLAMA_SPEC = TypeSpec("llama", [
    MetricDef("total_tokens", "Total tokens", "counter", "tok", "handler", "card"),
    MetricDef("input_tokens", "Input tokens", "counter", "tok", "handler", "card"),
    MetricDef("output_tokens", "Output tokens", "counter", "tok", "handler", "card"),
    MetricDef("tokens_per_sec", "Tokens / sec", "rate", "tok/s", "handler", "card",
              aggregation="avg"),
    MetricDef("prompt_tokens_per_sec", "Prompt tok / sec", "rate", "tok/s", "handler", "card",
              aggregation="avg"),
    MetricDef("generation_tokens_per_sec", "Generation tok / sec", "rate", "tok/s", "handler", "card",
              aggregation="avg"),
    MetricDef("requests", "Requests", "counter", "req", "handler", "card",
              aggregation="sum"),
    MetricDef("completed_requests", "Completed", "counter", "req", "handler", "card",
              aggregation="sum"),
])

OLLAMA_SPEC = TypeSpec("ollama", [
    MetricDef("requests", "Requests", "counter", "req", "handler", "card",
              aggregation="sum"),
    MetricDef("completed_requests", "Completed", "counter", "req", "handler", "card",
              aggregation="sum"),
    MetricDef("total_tokens", "Total tokens", "counter", "tok", "handler", "card"),
    MetricDef("tokens_per_sec", "Tokens / sec", "rate", "tok/s", "handler", "card",
              aggregation="avg"),
])

COMFYUI_SPEC = TypeSpec("comfyui", [
    MetricDef("queue_depth", "Queue depth", "gauge", "req", "handler", "card"),
    MetricDef("progress", "Progress", "gauge", "%", "handler", "card"),
    MetricDef("is_active", "Active", "text", "", "handler", "card"),
    MetricDef("idle_sec", "Idle (sec)", "gauge", "s", "sample", "card"),
])

TTS_SPEC = TypeSpec("tts", [
    MetricDef("requests", "Requests", "counter", "req", "handler", "card",
              aggregation="sum"),
    MetricDef("completed_requests", "Completed", "counter", "req", "handler", "card",
              aggregation="sum"),
    MetricDef("avg_latency_sec", "Avg latency", "gauge", "s", "handler", "card",
              aggregation="avg"),
])

# Fallback every application works with (spec 04 §8.1).
GENERIC_SPEC = TypeSpec("generic", [
    MetricDef("gpu_memory_mb", "GPU memory", "gauge", "MB", "sample", "card"),
    MetricDef("gpu_utilization_pct", "GPU utilisation", "gauge", "%", "sample", "card"),
    MetricDef("active_requests", "Active requests", "gauge", "req", "sample", "card"),
    MetricDef("queued_requests", "Queued requests", "gauge", "req", "sample", "card"),
])

# Common application metrics (spec 08 §4) — appended to every spec's view.
COMMON_METRICS = [
    MetricDef("active_requests", "Active requests", "gauge", "req", "sample", "card"),
    MetricDef("queued_requests", "Queued requests", "gauge", "req", "sample", "card"),
    MetricDef("requests_total", "Total requests", "counter", "req", "computed", "card",
              aggregation="sum"),
    MetricDef("requests_completed", "Successful", "counter", "req", "computed", "card",
              aggregation="sum"),
    MetricDef("requests_failed", "Failed", "counter", "req", "computed", "card",
              aggregation="sum"),
    MetricDef("avg_latency_sec", "Avg service", "gauge", "s", "computed", "card",
              aggregation="avg"),
    MetricDef("p50_wait_sec", "P50 wait", "histogram", "s", "computed", "card"),
    MetricDef("p95_wait_sec", "P95 wait", "histogram", "s", "computed", "card"),
    MetricDef("p99_wait_sec", "P99 wait", "histogram", "s", "computed", "card"),
    MetricDef("p50_service_sec", "P50 service", "histogram", "s", "computed", "card"),
    MetricDef("p95_service_sec", "P95 service", "histogram", "s", "computed", "card"),
    MetricDef("p99_service_sec", "P99 service", "histogram", "s", "computed", "card"),
    MetricDef("avg_wait_sec", "Avg wait", "gauge", "s", "computed", "card", aggregation="avg"),
    MetricDef("avg_service_sec", "Avg service", "gauge", "s", "computed", "card",
              aggregation="avg"),
]

BUILTIN_SPECS: dict[str, TypeSpec] = {
    "llama": LLAMA_SPEC,
    "ollama": OLLAMA_SPEC,
    "comfyui": COMFYUI_SPEC,
    "tts": TTS_SPEC,
    "generic": GENERIC_SPEC,
}


class TypeRegistry:
    """type → TypeSpec. ``custom`` resolves from the app config's metrics list."""

    def __init__(self) -> None:
        self._specs = dict(BUILTIN_SPECS)
        self._custom: dict[str, TypeSpec] = {}

    def spec_for(self, app_type: str, custom_metrics: list[dict] | None = None) -> TypeSpec:
        if app_type == "custom":
            if custom_metrics:
                spec = TypeSpec(
                    "custom",
                    [MetricDef(**{k: m.get(k, {}) for k in (
                        "id", "name", "kind", "unit", "source", "display", "collect",
                        "aggregation") if m.get(k) is not None}) for m in custom_metrics],
                )
                return spec
            return self._specs["generic"]
        return self._specs.get(app_type, self._specs["generic"])

    def register(self, spec: TypeSpec) -> None:
        self._specs[spec.type] = spec
