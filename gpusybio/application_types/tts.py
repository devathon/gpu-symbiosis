"""The ``tts`` application type (spec 04 §8): request-metrics spec served by the
generic handler (no TTS-specific collection logic today).
"""

from __future__ import annotations

from gpusybio.metrics.handlers import GenericHandler
from gpusybio.metrics.type_registry import TypePlugin
from gpusybio.metrics.typespec import MetricDef, TypeSpec

TTS_SPEC = TypeSpec("tts", [
    MetricDef("requests", "Total Requests", "counter", "", "handler", "card", "requests",
              aggregation="sum"),
    MetricDef("avg_latency_sec", "Avg latency", "gauge", "", "handler", "card", "latency",
              aggregation="avg"),
])

PLUGIN = TypePlugin("tts", TTS_SPEC, GenericHandler)
