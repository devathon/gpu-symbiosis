"""Pluggable application types (spec 04 §8).

An application type is a TypePlugin: a declared TypeSpec (what is recorded
and displayed) plus a handler factory (how it is fetched). The registry holds
NO type-specific code: each application type (llama, ollama, comfyui, tts)
lives in its own module under ``gpusybio/application_types/`` and registers
itself with the same contract as a drop-in ``<data_path>/plugins/`` module:

    register(registry)          — or —
    PLUGIN = TypePlugin(...)

Loading order (first-registered name wins):

    1. the generic fallback   (registered by make_type_registry)
    2. shipped type modules   (gpusybio/application_types/<type>.py)
    3. drop-in modules        (<data_path>/plugins/<name>.py)

A module that fails to import is logged and skipped — it never stops startup.

Unknown types fall back to the generic handler and spec, so every application
keeps working (spec 04 §8.1). ``custom`` resolves its spec from the
application's config (spec 08 §9.6).

The handler contract a type must fulfil (spec 04 §8):

required
    spec                                  — its registered fields
    handler.collect_metrics(app_id)      — per-tick values for its spec fields

optional
    handler.get_request_metrics(...)     — request completion metrics
    handler.detect_activity(...)         — GPU-activity interpretation
    spec.scheduled_patterns              — array of scheduled path regexes
                                           (gateway routing, spec 05 §6);
                                           should_schedule() derives from it
    handler.tailer(app_id)              — lifetime tailer: any object with
                                          read()/state()/seed(); the collector
                                          only ever calls state()/seed()
                                          (persist + seed), the handler calls
                                          read() inside collect_metrics.

Unified handler factory signature (spec 04 §8):

    (resources, states, request_metrics, base_urls, session=None,
     log_paths=None) -> handler
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
from dataclasses import dataclass

import gpusybio.application_types as _application_types
from gpusybio.events.log import get_logger
from gpusybio.metrics.handlers import GenericHandler
from gpusybio.metrics.typespec import GENERIC_SPEC, MetricDef, TypeSpec

log = get_logger("types")


@dataclass
class TypePlugin:
    type: str
    spec: TypeSpec
    handler: object  # factory with the unified signature above


class TypePluginRegistry:
    """type → (TypeSpec, handler factory). Unknown types fall back to the
    ``generic`` plugin (registered first by make_type_registry)."""

    def __init__(self) -> None:
        self._plugins: dict[str, TypePlugin] = {}

    def register(self, plugin: TypePlugin) -> bool:
        """Register a type. First-registered name wins (returns False when the
        name is already taken)."""
        if plugin.type in self._plugins:
            log.warning("type plugin %r already registered — ignored", plugin.type)
            return False
        self._plugins[plugin.type] = plugin
        return True

    def plugin_for(self, app_type: str) -> TypePlugin | None:
        return self._plugins.get(app_type)

    def spec_for(self, app_type: str, row=None) -> TypeSpec:
        plugin = self._plugins.get(app_type)
        if plugin is not None:
            return plugin.spec
        if app_type == "custom":
            custom = _custom_spec(row)
            if custom is not None:
                return custom
        return self._plugins.get("generic", TypePlugin("generic", GENERIC_SPEC,
                                                        GenericHandler)).spec

    def handler_for(self, app_type: str, resources, states, request_metrics,
                    base_urls, session=None, log_paths=None) -> object:
        plugin = self._plugins.get(app_type) or self._plugins.get("generic")
        factory = plugin.handler if plugin is not None else GenericHandler
        return factory(resources, states, request_metrics, base_urls, session,
                       log_paths=log_paths)

    def known(self) -> list[str]:
        return sorted(self._plugins)


def make_type_registry(data_path: str | None = None) -> TypePluginRegistry:
    """A registry wired the way the server uses it: the generic fallback first
    (so nothing can shadow it), then the shipped type modules, then drop-ins."""
    registry = TypePluginRegistry()
    registry.register(TypePlugin("generic", GENERIC_SPEC, GenericHandler))
    load_application_types(registry, data_path)
    return registry


def _apply_contract(registry: TypePluginRegistry, module, name: str) -> None:
    if callable(getattr(module, "register", None)):
        module.register(registry)
    elif isinstance(getattr(module, "PLUGIN", None), TypePlugin):
        registry.register(module.PLUGIN)
    else:
        log.warning("type plugin %s has no register() or PLUGIN — skipped", name)


def load_application_types(registry: TypePluginRegistry,
                          data_path: str | None = None) -> None:
    """Load application types (spec 04 §8): the shipped modules
    (``gpusybio/application_types/<type>.py``) first, then drop-in modules from
    ``<data_path>/plugins``. First-registered name wins."""
    pkg_dir = os.path.dirname(_application_types.__file__)
    for fn in sorted(os.listdir(pkg_dir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        name = fn[:-3]
        try:
            module = importlib.import_module(f"gpusybio.application_types.{name}")
            _apply_contract(registry, module, fn)
        except Exception:  # noqa: BLE001
            log.exception("application type %s failed to load — skipped", fn)
    if data_path:
        plugins_dir = os.path.join(data_path, "plugins")
        if not os.path.isdir(plugins_dir):
            return
        for fn in sorted(os.listdir(plugins_dir)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            path = os.path.join(plugins_dir, fn)
            try:
                spec = importlib.util.spec_from_file_location(
                    f"gpusybio_type_plugin_{fn[:-3]}", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _apply_contract(registry, module, fn)
            except Exception:  # noqa: BLE001
                log.exception("type plugin %s failed to load — skipped", fn)


def _custom_metrics_list(row) -> list[dict] | None:
    """The custom type's metric list (spec 08 §9.6), as declared dicts.

    Reads the application's dedicated ``metrics`` column (a JSON document,
    editable via the API without touching the human-readable description)
    first; falls back to the legacy ``description``-carried JSON for
    pre-migration rows that never set the column. The document is either the
    metric list itself or a ``{"metrics": [...]}`` wrapper (the legacy
    description shape); both are accepted."""
    for raw in (getattr(row, "metrics", None), getattr(row, "description", None)):
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            data = data.get("metrics")
        if isinstance(data, list) and data:
            return [m for m in data if isinstance(m, dict)] or None
    return None


def _custom_spec(row) -> TypeSpec | None:
    """Read a custom type's spec from its config (spec 08 §9.6)."""
    metrics = _custom_metrics_list(row)
    if metrics:
        return TypeSpec("custom", [
            MetricDef(**{k: m.get(k, {}) for k in (
                "id", "name", "kind", "unit", "source", "display", "collect",
                "aggregation") if isinstance(m, dict) and m.get(k) is not None})
            for m in metrics if isinstance(m, dict) and m.get("id")
        ])
    return None
