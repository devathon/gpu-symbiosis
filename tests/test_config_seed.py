"""Seed semantics (spec 16 §2.6): the seed creates missing application/gateway
rows only; an existing row is updated at startup only when its spec sets
``update: true``. Settings always overwrite (no settings API exists). The seed
never deletes rows absent from the document.
"""

from __future__ import annotations

import json

from gpusybio.config.models import ApplicationSpec, Config, GatewaySpec
from gpusybio.config.seed import seed_database
from gpusybio.database.db import create_engine, init_db, make_session_factory
from gpusybio.database.models import Application, Gateway, Setting


def _db(tmp_path):
    engine = create_engine(str(tmp_path / "seed.db"))
    init_db(engine)
    return make_session_factory(engine)


def _app_spec(update: bool = False, **overrides) -> ApplicationSpec:
    fields = dict(
        id="a1",
        name="qwen38",
        start_script="echo start",
        stop_script="echo stop",
        port=9501,
        gpu_memory_mb=18432,
        update=update,
    )
    fields.update(overrides)
    return ApplicationSpec(**fields)


def _config(update: bool = False) -> Config:
    return Config(
        applications=[_app_spec(update=update)],
        gateways=[
            GatewaySpec(id="gw-a1", name="gw-a1", port=9511, application_id="a1", update=update)
        ],
    )


def test_seed_creates_missing_rows(tmp_path):
    db = _db(tmp_path)
    seed_database(db, _config())

    with db() as s:
        a = s.get(Application, "a1")
        g = s.get(Gateway, "gw-a1")
    assert a is not None and g is not None
    assert a.name == "qwen38"
    assert a.port == 9501
    assert a.start_script == "echo start"
    assert a.max_concurrent_requests == 1
    # Derived: unloadable iff it has an unload script (none here).
    assert a.supports_load_unload is False
    # Runtime-mutated fields initialize from the spec on a NEW row only.
    assert a.observed_max_gpu_memory_mb == 18432
    assert g.port == 9511
    assert g.application_id == "a1"


def test_seed_skips_existing_app_by_default(tmp_path):
    db = _db(tmp_path)
    seed_database(db, _config())

    # Runtime change (via API) on the existing row.
    with db() as s:
        a = s.get(Application, "a1")
        a.name = "renamed-via-api"
        a.port = 9601
        a.max_concurrent_requests = 8
    # Re-seed with the same spec (update flag defaults to false).
    seed_database(db, _config())

    with db() as s:
        a = s.get(Application, "a1")
    # The runtime change survives the re-seed.
    assert a.name == "renamed-via-api"
    assert a.port == 9601
    assert a.max_concurrent_requests == 8


def test_seed_updates_existing_app_with_update_true(tmp_path):
    db = _db(tmp_path)
    seed_database(db, _config())

    # Runtime change (via API) on the existing row.
    with db() as s:
        a = s.get(Application, "a1")
        a.name = "renamed-via-api"
        a.port = 9601
        a.max_concurrent_requests = 8
    # Re-seed with update: true — the spec wins.
    seed_database(db, _config(update=True))

    with db() as s:
        a = s.get(Application, "a1")
    assert a.name == "qwen38"
    assert a.port == 9501
    assert a.max_concurrent_requests == 1


def test_seed_skips_existing_gateway_by_default(tmp_path):
    db = _db(tmp_path)
    seed_database(db, _config())

    with db() as s:
        g = s.get(Gateway, "gw-a1")
        g.name = "renamed-via-api"
        g.priority = 99
    seed_database(db, _config())

    with db() as s:
        g = s.get(Gateway, "gw-a1")
    assert g.name == "renamed-via-api"
    assert g.priority == 99


def test_seed_updates_existing_gateway_with_update_true(tmp_path):
    db = _db(tmp_path)
    seed_database(db, _config())

    with db() as s:
        g = s.get(Gateway, "gw-a1")
        g.name = "renamed-via-api"
        g.priority = 99
    seed_database(db, _config(update=True))

    with db() as s:
        g = s.get(Gateway, "gw-a1")
    assert g.name == "gw-a1"
    assert g.priority == 5  # GatewaySpec default


def test_settings_always_overwrite(tmp_path):
    db = _db(tmp_path)
    cfg = _config()
    seed_database(db, cfg)

    with db() as s:
        row = s.get(Setting, "load.max_retries")
    assert row is not None
    assert json.loads(row.value) == cfg.settings.load.max_retries

    # A runtime change to the setting (no API exists, but the DB is the store).
    with db() as s:
        row = s.get(Setting, "load.max_retries")
        row.value = json.dumps(99)
    # Re-seed — settings always overwrite, so the YAML value wins.
    seed_database(db, cfg)

    with db() as s:
        row = s.get(Setting, "load.max_retries")
    assert json.loads(row.value) == cfg.settings.load.max_retries


def test_supports_load_unload_derived_from_unload_script(tmp_path):
    """supports_load_unload is derived from ``unload_script`` at read time. The
    stored flag is a cache and can go stale (e.g. the script set directly in the
    DB) — a stale flag must not hide the UI's Unload button or the manager's
    unload path. Regression for production: comfyui had an unload script but a
    stale ``supports_load_unload=false``."""
    from gpusybio.api import _app_view
    from gpusybio.applications.manager import AppConfig
    from gpusybio.config.models import GpuSpec, SettingsSpec
    from gpusybio.events.bus import EventBus
    from gpusybio.resources.gpu import make_backend
    from gpusybio.resources.manager import ResourceManager
    from gpusybio.state import StateManager

    db = _db(tmp_path)
    # Set the unload script directly on the row (bypassing the API), leaving the
    # stored flag False — the exact stale state observed in production.
    with db() as s:
        s.add(Application(
            id="comfyui", name="ComfyUI", start_script="echo", port=8188,
            configured_gpu_memory_mb=1024,
            unload_script="curl -X POST http://127.0.0.1:8188/api/free",
            supports_load_unload=False,
        ))
    with db() as s:
        row = s.get(Application, "comfyui")
    # The stored column is stale (False) even though the script is set.
    assert row.unload_script is not None
    assert row.supports_load_unload is False

    # Both read sites must derive True from the script — the manager's
    # unload()/release() path and the UI's app view.
    assert AppConfig.from_row(row).supports_load_unload is True
    bus = EventBus()
    resources = ResourceManager(
        backend=make_backend(GpuSpec(backend="fake", fake_total_mb=24576)), bus=bus
    )
    view = _app_view(row, StateManager().get("comfyui"), resources)
    assert view["supports_load_unload"] is True

    # And the inverse: no script → not unloadable, even if the flag says true.
    with db() as s:
        row = s.get(Application, "comfyui")
        row.unload_script = None
        row.supports_load_unload = True
    assert AppConfig.from_row(row).supports_load_unload is False
