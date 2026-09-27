"""Startup loads ALL persisted applications/gateways from the DB (source of
truth), not just the seed document — so API-created apps survive a restart
(spec 16 §2.6). Regression for: starting an app created via the API (in the DB,
absent from config.yaml) after a fresh start raised ``KeyError`` in
``ApplicationManager.config``.
"""

from __future__ import annotations

from gpusybio.config.models import ApplicationSpec, Config, GpuSpec, SettingsSpec
from gpusybio.database.db import create_engine, init_db, make_session_factory
from gpusybio.database.models import Application, Gateway
from gpusybio.server import Server


async def test_app_in_db_not_in_seed_is_loaded(tmp_path):
    db_path = str(tmp_path / "restart.db")
    # Pre-seed the DB with an app + gateway that are NOT in the config we hand
    # to Server (as POST /api/applications + POST /api/gateways would have done).
    engine = create_engine(db_path)
    init_db(engine)
    db = make_session_factory(engine)
    with db() as s:
        s.add(Application(
            id="comfyui", name="ComfyUI", start_script="echo comfy",
            port=9010, configured_gpu_memory_mb=1024,
        ))
        s.add(Gateway(
            id="gw-comfyui", name="gw-comfyui", port=9020, application_id="comfyui",
        ))

    cfg = Config(
        db_path=db_path,
        admin_port=9999,
        # 'a1' is the only app in the seed document.
        applications=[
            ApplicationSpec(id="a1", name="a1", start_script="echo a1",
                           port=9001, gpu_memory_mb=1024),
        ],
        settings=SettingsSpec(gpu=GpuSpec(backend="fake", fake_total_mb=24576)),
    )
    srv = Server(cfg)
    try:
        # DB is the source of truth: both the pre-existing app and the seeded
        # app must be loaded — not just the seed document's apps.
        assert "comfyui" in srv.app_ids
        assert "a1" in srv.app_ids
        # The reported crash: apps.config('comfyui') must not raise KeyError.
        assert srv.apps.config("comfyui").port == 9010
        assert srv.apps.config("a1").port == 9001
        # Gateway configs are derived from the DB too (request manager).
        assert "gw-comfyui" in srv.gateway_ids
        assert srv.request_manager.gateway("gw-comfyui")["application_id"] == "comfyui"
    finally:
        srv.engine.dispose()
        engine.dispose()


async def test_config_lazy_loads_missing_app(tmp_path):
    """ApplicationManager.config falls back to a DB load if the cache is empty."""
    db_path = str(tmp_path / "lazy.db")
    engine = create_engine(db_path)
    init_db(engine)
    db = make_session_factory(engine)
    with db() as s:
        s.add(Application(
            id="late", name="Late", start_script="echo late",
            port=9030, configured_gpu_memory_mb=1024,
        ))

    from gpusybio.applications.manager import ApplicationManager
    from gpusybio.state import StateManager
    from gpusybio.events.bus import EventBus
    from gpusybio.resources.gpu import make_backend
    from gpusybio.resources.manager import ResourceManager

    bus = EventBus()
    resources = ResourceManager(
        backend=make_backend(GpuSpec(backend="fake", fake_total_mb=24576)),
        bus=bus,
    )
    apps = ApplicationManager(StateManager(), resources, bus, db,
                             SettingsSpec().scheduler)
    try:
        # Not loaded into the cache yet — config() must lazy-load from the DB.
        assert "late" not in apps._configs
        assert apps.config("late").port == 9030
        assert "late" in apps._configs
    finally:
        engine.dispose()
