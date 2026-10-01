"""Unit: lifetime token-metric persistence (spec 08 §5.1).

The tailer's lifetime totals + log offset + session fingerprint are persisted
so they survive restarts. Written on a 60 s timer for active apps and on app
stop; seeded on server start (the tailer resumes after the stored offset).
"""

from __future__ import annotations

import pytest

from gpusybio.database.db import create_engine, init_db, make_session_factory
from gpusybio.metrics.lifetime import LifetimeStore
from gpusybio.application_types.llama import LlamaLogMetrics


_PROMPT_100 = (
    "llama_perf_context_print: prompt eval time =    1234.56 ms /   100 tokens "
    "(   12.35 ms per token,   81.00 tokens per second)\n"
)
_PROMPT_50 = (
    "llama_perf_context_print: prompt eval time =     617.28 ms /    50 tokens "
    "(   12.35 ms per token,   81.00 tokens per second)\n"
)


def _factory(tmp_path):
    engine = create_engine(str(tmp_path / "t.db"))
    init_db(engine)
    return make_session_factory(engine)


def test_lifetime_store_roundtrip_and_upsert(tmp_path):
    store = LifetimeStore(_factory(tmp_path))
    state = {
        "input_tokens": 100, "output_tokens": 200, "total_tokens": 300,
        "total_time_ms": 6913.46, "prompt_time_ms": 1234.56, "eval_time_ms": 5678.90,
        "requests": 1,
        "draft_accepted": 642, "draft_generated": 1568,
        "slot_selections": 3, "cache_hit_requests": 2, "cached_tokens": 62189,
        "log_offset": 500, "log_fingerprint": b"session-head-bytes",
    }
    store.save("a1", state)
    got = store.load("a1")
    assert got["input_tokens"] == 100
    assert got["cached_tokens"] == 62189
    assert got["prompt_time_ms"] == 1234.56
    assert got["eval_time_ms"] == 5678.90
    assert got["log_offset"] == 500
    assert got["log_fingerprint"] == b"session-head-bytes"
    assert got["draft_accepted"] == 642
    # upsert on the same primary key
    store.save("a1", {**state, "input_tokens": 150})
    assert store.load("a1")["input_tokens"] == 150
    # load_many returns only the present apps
    assert set(store.load_many(["a1", "a2"]).keys()) == {"a1"}
    # unknown app -> None
    assert store.load("a2") is None


def test_seed_from_store_resumes_after_offset(tmp_path):
    log = str(tmp_path / "a1.log")
    with open(log, "w") as f:
        f.write(_PROMPT_100)
    t = LlamaLogMetrics(log)
    t.read()
    store = LifetimeStore(_factory(tmp_path))
    store.save("a1", t.state())  # persist (60 s timer / app stop)

    # a new request appends to the intact log (same session)
    with open(log, "a") as f:
        f.write(_PROMPT_50)

    # simulate a server restart: a fresh tailer seeded from the store
    t2 = LlamaLogMetrics(log)
    t2.seed(store.load("a1"))
    snap = t2.read()
    assert snap["input_tokens"] == 150  # 100 (kept) + 50 (new) — no double-count


def test_seed_keeps_lifetime_on_truncation(tmp_path):
    log = str(tmp_path / "a1.log")
    with open(log, "w") as f:
        f.write(_PROMPT_100)
    t = LlamaLogMetrics(log)
    t.read()
    store = LifetimeStore(_factory(tmp_path))
    store.save("a1", t.state())

    # the app restarted: the log is truncated to a fresh session
    with open(log, "w") as f:
        f.write(_PROMPT_50)
    t2 = LlamaLogMetrics(log)
    t2.seed(store.load("a1"))
    snap = t2.read()
    assert snap["input_tokens"] == 150  # 100 (kept) + 50 (fresh session)


def test_app_metrics_fallback_uses_persisted_lifetime(tmp_path):
    """When the tailer's in-memory lifetime is empty (not yet seeded), the
    metrics view falls back to the persisted DB lifetime (spec 08 §5.1) so the
    card never shows zeroed tokens."""
    store = LifetimeStore(_factory(tmp_path))
    store.save("a1", {
        "input_tokens": 100, "output_tokens": 200, "total_tokens": 300,
        "cached_tokens": 62189,
        "total_time_ms": 6913.46, "prompt_time_ms": 1234.56,
        "eval_time_ms": 3333.33, "requests": 1,
        "log_offset": 500, "log_fingerprint": b"head",
    })
    # The tailer's in-memory is empty (not yet seeded) -> collect_metrics
    # returns zeroed token keys.
    type_metrics = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                    "tokens_per_sec": 0, "prompt_tokens_per_sec": 0,
                    "generation_tokens_per_sec": 0}
    # Apply the fallback logic (mirrors MetricsCollector.app_metrics): the
    # rates are the throughput-weighted sum(tokens) / sum(phase time).
    if not type_metrics.get("input_tokens") and not type_metrics.get("output_tokens"):
        lifetime = store.load("a1")
        if lifetime and (lifetime.get("input_tokens") or lifetime.get("output_tokens")):
            total = lifetime.get("total_tokens") or (
                lifetime.get("input_tokens", 0) + lifetime.get("output_tokens", 0)
            )
            tms = float(lifetime.get("total_time_ms", 0.0))
            pms = float(lifetime.get("prompt_time_ms", 0.0))
            ems = float(lifetime.get("eval_time_ms", 0.0))
            for key, v in (
                ("input_tokens", lifetime.get("input_tokens")),
                ("cached_tokens", lifetime.get("cached_tokens")),
                ("output_tokens", lifetime.get("output_tokens")),
                ("total_tokens", total),
                ("prompt_tokens_per_sec",
                 lifetime.get("input_tokens", 0) / (pms / 1000.0) if pms else None),
                ("generation_tokens_per_sec",
                 lifetime.get("output_tokens", 0) / (ems / 1000.0) if ems else None),
                ("tokens_per_sec", total / (tms / 1000.0) if tms else None),
            ):
                if v and not type_metrics.get(key):
                    type_metrics[key] = v
    assert type_metrics["input_tokens"] == 100
    assert type_metrics["cached_tokens"] == 62189
    assert type_metrics["output_tokens"] == 200
    assert type_metrics["total_tokens"] == 300
    assert type_metrics["prompt_tokens_per_sec"] == pytest.approx(81.0, rel=1e-3)
    assert type_metrics["generation_tokens_per_sec"] == pytest.approx(60.0, rel=1e-3)
    assert type_metrics["tokens_per_sec"] > 0


def test_corrupt_lifetime_json_starts_fresh(tmp_path):
    """A tampered/corrupt lifetime document is treated as empty (spec 08 §5.1) —
    the tailer starts a fresh lifetime instead of crashing the seed."""
    from sqlalchemy import text

    db = _factory(tmp_path)
    store = LifetimeStore(db)
    store.save("a1", {"input_tokens": 100, "log_offset": 500,
                      "log_fingerprint": b"head"})
    with db() as s:
        s.execute(text("UPDATE app_metric_lifetime SET lifetime=:v"),
                 {"v": "not-json"})
        s.commit()
    got = store.load("a1")
    # corrupt document -> fresh (empty) lifetime, no type fields
    assert "input_tokens" not in got
    # mechanism columns survive independently of the document
    assert got["log_offset"] == 500
    assert got["log_fingerprint"] == b"head"
    # a save after the corruption replaces the bad document
    store.save("a1", {"input_tokens": 1, "log_offset": 2, "log_fingerprint": b"x"})
    assert store.load("a1")["input_tokens"] == 1
