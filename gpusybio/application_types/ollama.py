"""The ``ollama`` application type (spec 04 §8): Ollama logs the same
llama.cpp per-request lines, so its handler is the llama module's — reused by
explicit import, not duplicated.
"""

from __future__ import annotations

from gpusybio.application_types.llama import LlamaHandler
from gpusybio.metrics.type_registry import TypePlugin
from gpusybio.metrics.typespec import MetricDef, TypeSpec

OLLAMA_SPEC = TypeSpec("ollama", [
    MetricDef("requests", "Total Requests", "counter", "", "handler", "card", "requests",
              aggregation="sum"),
    MetricDef("tokens_per_sec", "Tokens t/s", "rate", "", "handler", "card", "rates",
              aggregation="avg"),
])


def register(registry) -> None:
    registry.register(TypePlugin("ollama", OLLAMA_SPEC, LlamaHandler))
