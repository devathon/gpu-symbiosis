"""Unit: pluggable application types (spec 04 §8).

The type registry holds no type-specific code: the shipped type modules
(gpusybio/application_types/<type>.py) and drop-in modules
(<data_path>/plugins) register themselves with the same contract; the generic
fallback is registered first so nothing can shadow it. Unknown types fall back
to the generic handler; a broken module never stops startup.
"""

from __future__ import annotations

import json

import pytest

from gpusybio.metrics.type_registry import (
    TypePlugin,
    make_type_registry,
    load_application_types,
)
from gpusybio.metrics.handlers import GenericHandler
from gpusybio.metrics.typespec import GENERIC_SPEC, MetricDef, TypeSpec
from gpusybio.application_types.llama import LLAMA_SPEC, LlamaHandler
from gpusybio.application_types.ollama import OLLAMA_SPEC
from gpusybio.application_types.comfyui import COMFYUI_SPEC, ComfyUIHandler
from gpusybio.application_types.tts import TTS_SPEC


def test_loaded_application_types():
    reg = make_type_registry()
    # shipped type modules: each resolves to its module's spec + handler
    assert reg.spec_for("llama") is LLAMA_SPEC
    assert reg.spec_for("ollama") is OLLAMA_SPEC
    assert reg.spec_for("comfyui") is COMFYUI_SPEC
    assert reg.spec_for("tts") is TTS_SPEC
    assert isinstance(reg.handler_for("llama", None, None, None, {}), LlamaHandler)
    assert isinstance(reg.handler_for("comfyui", None, None, None, {}), ComfyUIHandler)
    # tts has its own spec but the generic handler (no TTSHandler today)
    assert reg.spec_for("tts").type == "tts"
    assert isinstance(reg.handler_for("tts", None, None, None, {}), GenericHandler)
    # the generic fallback is registered (and can't be shadowed)
    assert "generic" in reg.known()
    assert isinstance(reg.handler_for("generic", None, None, None, {}), GenericHandler)
    # unknown type falls back to the generic handler + spec
    assert isinstance(reg.handler_for("nonsense", None, None, None, {}), GenericHandler)
    assert reg.spec_for("nonsense") is GENERIC_SPEC
    assert reg.plugin_for("nonsense") is None
    # custom is not a registered plugin: its spec comes from the app config
    assert reg.plugin_for("custom") is None
    assert isinstance(reg.handler_for("custom", None, None, None, {}), GenericHandler)


def test_custom_spec_from_app_config():
    reg = make_type_registry()
    metrics = [
        {"id": "queue_depth", "name": "Queue depth", "unit": "req", "kind": "gauge",
         "source": "handler", "display": "card",
         "collect": {"url": "/stats", "json_path": "queue.depth"}},
    ]
    # the dedicated ``metrics`` column (a JSON document) is the primary source —
    # a plain human description alongside it is ignored
    row = type("Row", (), {"metrics": json.dumps(metrics), "description": "My app"})
    spec = reg.spec_for("custom", row)
    assert spec.type == "custom"
    assert spec.metric("queue_depth").collect["url"] == "/stats"
    # a human description alone -> generic (no declared metrics)
    assert reg.spec_for(
        "custom", type("Row", (), {"metrics": None, "description": "My app"})) is GENERIC_SPEC
    # legacy: the description-carried {"metrics": [...]} JSON still works
    row = type("Row", (), {"metrics": None,
                          "description": json.dumps({"metrics": metrics})})
    spec = reg.spec_for("custom", row)
    assert spec.type == "custom"
    assert spec.metric("queue_depth").collect["url"] == "/stats"
    # no metrics / no description -> generic
    assert reg.spec_for("custom", None) is GENERIC_SPEC
    assert reg.spec_for("custom", type("Row", (), {"description": None})) is GENERIC_SPEC
    assert reg.spec_for("custom", type("Row", (), {"description": "not json"})) is GENERIC_SPEC


def test_plugin_register_and_resolve():
    class MyHandler:
        def __init__(self, resources, states, request_metrics=None, base_urls=None,
                     session=None, log_paths=None):
            pass

        async def collect_metrics(self, app_id: str) -> dict:
            return {"widgets": 42}

    reg = make_type_registry()
    plugin = TypePlugin(
        "widgets",
        TypeSpec("widgets", [MetricDef("widgets", "Widgets", "gauge", "", "handler", "card",
                                        "widgets", aggregation="last")]),
        MyHandler,
    )
    assert reg.register(plugin) is True
    assert "widgets" in reg.known()
    assert reg.spec_for("widgets").metric("widgets") is not None
    assert reg.spec_for("widgets").metric("widgets").group == "widgets"
    assert isinstance(reg.handler_for("widgets", None, None, None, {}), MyHandler)


def test_shipped_type_wins_over_dropin():
    """First-registered name wins: a drop-in can't shadow a shipped type."""
    reg = make_type_registry()
    plugin = TypePlugin("llama", TypeSpec("llama", []), GenericHandler)
    assert reg.register(plugin) is False
    assert reg.spec_for("llama") is LLAMA_SPEC
    assert isinstance(reg.handler_for("llama", None, None, None, {}), LlamaHandler)
    # neither can a drop-in shadow the generic fallback
    assert reg.register(TypePlugin("generic", TypeSpec("generic", []), GenericHandler)) is False


# -- drop-in discovery (spec 04 §8) ---------------------------------------------
def test_dropin_load_register_function(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "widgets.py").write_text(
        "from gpusybio.metrics.type_registry import TypePlugin\n"
        "from gpusybio.metrics.typespec import TypeSpec, MetricDef\n"
        "\n"
        "class W:\n"
        "    def __init__(self, *a, **kw):\n"
        "        pass\n"
        "    def collect_metrics(self, app_id):\n"
        "        return {'widgets': 1}\n"
        "\n"
        "def register(registry):\n"
        "    registry.register(TypePlugin('widgets', TypeSpec('widgets', [\n"
        "        MetricDef('widgets', 'Widgets', 'gauge')]), W))\n"
    )
    reg = make_type_registry(str(tmp_path))
    assert "widgets" in reg.known()
    handler = reg.handler_for("widgets", None, None, None, {})
    assert handler.__class__.__name__ == "W"


def test_dropin_load_plugin_attribute(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "gadgets.py").write_text(
        "from gpusybio.metrics.type_registry import TypePlugin\n"
        "from gpusybio.metrics.handlers import GenericHandler\n"
        "from gpusybio.metrics.typespec import TypeSpec, MetricDef\n"
        "\n"
        "PLUGIN = TypePlugin('gadgets', TypeSpec('gadgets', [\n"
        "    MetricDef('gizmos', 'Gizmos', 'gauge')]), GenericHandler)\n"
    )
    reg = make_type_registry(str(tmp_path))
    assert "gadgets" in reg.known()
    assert reg.spec_for("gadgets").metric("gizmos") is not None


def test_dropin_broken_module_isolated(tmp_path):
    """A plugin that fails to import is skipped; the others still load."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "zz_broken.py").write_text("raise RuntimeError('boom')\n")
    (plugins / "aa_good.py").write_text(
        "from gpusybio.metrics.type_registry import TypePlugin\n"
        "from gpusybio.metrics.handlers import GenericHandler\n"
        "from gpusybio.metrics.typespec import TypeSpec, MetricDef\n"
        "\n"
        "def register(registry):\n"
        "    registry.register(TypePlugin('goodtype', TypeSpec('goodtype', [\n"
        "        MetricDef('ok', 'OK', 'gauge')]), GenericHandler))\n"
    )
    (plugins / "no_contract.py").write_text("VALUE = 1\n")
    (plugins / "_skipme.py").write_text("VALUE = 2\n")
    reg = make_type_registry(str(tmp_path))  # must not raise
    assert "goodtype" in reg.known()
    assert "skipme" not in reg.known()  # underscore-prefixed files are skipped


def test_dropin_missing_dir_is_noop(tmp_path):
    reg = make_type_registry(str(tmp_path))  # no plugins dir — no error
    assert reg.known() == ["comfyui", "generic", "llama", "ollama", "tts"]


# -- config gate (spec 04 §8) ----------------------------------------------------
def test_config_model_accepts_arbitrary_type():
    """The config no longer rejects unknown application types (free-form)."""
    from gpusybio.config.models import ApplicationSpec, AppType

    assert AppType is str
    app = ApplicationSpec(id="x", name="x", start_script="s", port=1,
                          gpu_memory_mb=1, application_type="mynewtype")
    assert app.application_type == "mynewtype"


# -- spec-driven view (spec 08 §9) -----------------------------------------------
def test_metrics_view_groups_and_requests_dedupe():
    from gpusybio.metrics import _metrics_view

    values = {
        "input_tokens": 100, "cached_tokens": 5000, "output_tokens": 50,
        "tokens_per_sec": 1.0,
        "prompt_tokens_per_sec": 81.0, "generation_tokens_per_sec": 60.0,
        "draft_acceptance": 43.8, "cache_hit_pct": 66.7,
        "requests": 7, "requests_received": 12, "requests_failed": 1,
        "p50_wait_sec": 1.0, "p95_wait_sec": 2.0, "p99_wait_sec": 3.0,
        "p50_service_sec": 0.5, "p95_service_sec": 1.0, "p99_service_sec": 1.5,
    }
    view = _metrics_view(LLAMA_SPEC, values)
    ids = [v["id"] for v in view]
    # requests dedupe: the log-derived count wins, shown once
    assert ids.count("requests") == 1
    assert "requests_received" not in ids
    assert [v for v in view if v["id"] == "requests"][0]["value"] == 7
    # groups come from the type's spec (spec 08 §9)
    # cached input is the second card, right after input tokens
    assert ids.index("cached_tokens") == ids.index("input_tokens") + 1
    groups = {v["id"]: v["group"] for v in view}
    assert groups["input_tokens"] == "tokens"
    assert groups["cached_tokens"] == "tokens"
    assert groups["prompt_tokens_per_sec"] == "rates"
    assert groups["draft_acceptance"] == "speculative"
    assert groups["requests"] == "requests"
    assert groups["requests_failed"] == "requests"
    assert groups["p95_wait_sec"] == "latency"

    # a type without a log (generic) shows the gateway count as "Requests"
    view2 = _metrics_view(GENERIC_SPEC, {"requests_received": 12, "requests_failed": 0})
    ids2 = [v["id"] for v in view2]
    assert "requests_received" in ids2
    assert [v for v in view2 if v["id"] == "requests_received"][0]["value"] == 12
    assert [v for v in view2 if v["id"] == "requests_received"][0]["group"] == "requests"
