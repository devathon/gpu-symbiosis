"""The ``shell-command`` application type (per-request command runner).

The app is a normal long-running server: the framework's start path launches the
built-in command server (``gpusybio.command_server``), which runs the configured
``start_script`` template for every POST request (other methods are 405). Routing
is the default (every request is scheduled - they all do work); metrics are
request metrics + process/GPU, served by the generic handler (no type-specific
collection).
"""

from __future__ import annotations

from gpusybio.metrics.handlers import GenericHandler
from gpusybio.metrics.type_registry import TypePlugin
from gpusybio.metrics.typespec import MetricDef, TypeSpec

SHELL_COMMAND_SPEC = TypeSpec("shell-command", [
    MetricDef("requests", "Total Requests", "counter", "", "handler", "card", "requests",
              aggregation="sum"),
    MetricDef("avg_latency_sec", "Avg latency", "gauge", "", "handler", "card", "latency",
              aggregation="avg"),
])

PLUGIN = TypePlugin("shell-command", SHELL_COMMAND_SPEC, GenericHandler)
