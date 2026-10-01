"""Type metrics contract (spec 08 §9): what is recorded/displayed is declared by
a TypeSpec; how it is fetched is the TypeHandler's job.

MetricDef is a pure dataclass — the UI auto-renders from it. Application types
(llama, ollama, comfyui, tts) each live in their own module under
``gpusybio/application_types/`` (spec 04 §8); this module holds only the
type-agnostic contract: ``custom`` reads its metric list from the application's
config and ``generic`` is the fallback every application works with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class MetricDef:
    id: str
    name: str
    kind: str = "gauge"  # gauge | rate | counter | histogram | text
    unit: str = ""
    source: str = "sample"  # sample | handler | config | computed
    display: str = "card"  # card | chart | table | text
    group: str = "metrics"  # card section the UI renders the field under
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

    def __post_init__(self) -> None:
        # spec 05 §6: compile the scheduled-path regexes once. A subclass may
        # declare the ``scheduled_patterns`` class attribute — an array of path
        # regexes. Absent/None means every request is scheduled (the default).
        patterns = getattr(self, "scheduled_patterns", None)
        self._scheduled_patterns = (
            None if patterns is None else [re.compile(p) for p in patterns]
        )

    # spec 05 §6: which requests go through the scheduler (queue / LB / demand)
    # vs pass through directly. A request is scheduled when its path matches any
    # entry of the ``scheduled_patterns`` regex array (re.search); with no array
    # every request is scheduled. ``method`` is part of the contract (drop-in
    # types may use it) but the default decision is path-based only.
    def should_schedule(self, method: str, path: str) -> bool:
        if self._scheduled_patterns is None:
            return True
        return any(p.search(path) for p in self._scheduled_patterns)


# -- type-agnostic spec (spec 04 §8.1) ------------------------------------------

# Fallback every application works with (spec 04 §8.1). GPU memory/utilisation are
# shown in the applications list (not the metrics card). Application types live
# in gpusybio/application_types/<type>.py (spec 04 §8).
GENERIC_SPEC = TypeSpec("generic", [
    MetricDef("requests_received", "Total Requests", "counter", "", "computed", "card",
              "requests", aggregation="sum"),
])

# Common application metrics (spec 08 §4) — appended to every spec's view. Values
# are rendered as bare numbers; the UI condenses the latency percentiles into
# "Wait (P50/95/99)" / "Service (P50/95/99)" and combines active + queued into
# "Active Requests".
COMMON_METRICS = [
    MetricDef("requests_failed", "Failed Requests", "counter", "", "computed", "card",
              "requests", aggregation="sum"),
    MetricDef("p50_wait_sec", "P50 wait", "histogram", "", "computed", "card", "latency"),
    MetricDef("p95_wait_sec", "P95 wait", "histogram", "", "computed", "card", "latency"),
    MetricDef("p99_wait_sec", "P99 wait", "histogram", "", "computed", "card", "latency"),
    MetricDef("p50_service_sec", "P50 service", "histogram", "", "computed", "card", "latency"),
    MetricDef("p95_service_sec", "P95 service", "histogram", "", "computed", "card", "latency"),
    MetricDef("p99_service_sec", "P99 service", "histogram", "", "computed", "card", "latency"),
    MetricDef("avg_wait_sec", "Avg wait", "gauge", "", "computed", "card", "latency",
              aggregation="avg"),
    MetricDef("avg_service_sec", "Avg service", "gauge", "", "computed", "card", "latency",
              aggregation="avg"),
]
