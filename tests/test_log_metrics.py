"""Unit: the llama.cpp log tailer (spec 08 §5).

The application's stdout is the log; the metrics handler tails it for the
per-request token lines (``llama_perf_context_print``) and accumulates the
lifetime totals. These tests exercise the tailer directly (offset tracking,
cumulative totals, the average tokens-per-second, and rotation safety).
"""

from __future__ import annotations

import os

import pytest

from gpusybio.application_types.llama import LlamaLogMetrics


# Two requests in the confirmed llama.cpp format (AI/dflash2/llama.cpp
# llama_perf_context_print): a prompt line (input tokens), an eval line (output
# tokens, "runs"), and a total line.
_T1 = (
    "llama_perf_context_print: prompt eval time =    1234.56 ms /   100 tokens "
    "(   12.35 ms per token,   81.00 tokens per second)\n"
    "llama_perf_context_print:        eval time =    5678.90 ms /   200 runs   "
    "(   28.39 ms per token,   35.22 tokens per second)\n"
    "llama_perf_context_print:       total time =    6913.46 ms /   300 tokens\n"
)
_T2 = (
    "llama_perf_context_print: prompt eval time =     617.28 ms /    50 tokens "
    "(   12.35 ms per token,   81.00 tokens per second)\n"
    "llama_perf_context_print:        eval time =    2839.45 ms /   100 runs   "
    "(   28.39 ms per token,   35.22 tokens per second)\n"
    "llama_perf_context_print:       total time =    3456.73 ms /   150 tokens\n"
)


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def test_taler_accumulates_totals_and_average_speed(tmp_path):
    p = str(tmp_path / "a1.log")
    _write(p, _T1)
    t = LlamaLogMetrics(p)
    snap = t.read()
    assert snap["input_tokens"] == 100
    assert snap["output_tokens"] == 200
    assert snap["total_tokens"] == 300
    assert snap["requests"] == 1
    assert snap["completed_requests"] == 1
    # prompt / generation tokens-per-second, throughput-weighted
    # (sum(tokens) / sum(time) — identical to the per-line rates here since
    # every line has the same rate)
    assert abs(snap["prompt_tokens_per_sec"] - 81.00) < 0.01
    assert abs(snap["generation_tokens_per_sec"] - 35.22) < 0.01
    # total throughput from the total line
    assert snap["tokens_per_sec"] > 0
    assert not snap["stale"]

    # a second request appends to the lifetime totals (offset is tracked)
    with open(p, "a") as f:
        f.write(_T2)
    snap2 = t.read()
    assert snap2["input_tokens"] == 150
    assert snap2["output_tokens"] == 300
    assert snap2["total_tokens"] == 450
    assert snap2["requests"] == 2

    # no new lines -> same totals (offset unchanged)
    snap3 = t.read()
    assert snap3["input_tokens"] == 150


def test_tps_is_throughput_weighted_not_per_request_mean(tmp_path):
    """The rate is sum(tokens) / sum(time), NOT the arithmetic mean of the
    per-request rates — a large prompt weighs by its work, not one vote."""
    p = str(tmp_path / "a1.log")
    # Two requests with very different prompt rates: a 103,790-token prompt at
    # 997.85 t/s and a 110-token prompt at 260.94 t/s.
    _write(p,
        "llama_perf_context_print: prompt eval time =  104013.32 ms /  103790 tokens "
        "(    1.00 ms per token,   997.85 tokens per second)\n"
        "llama_perf_context_print:        eval time =   20172.89 ms /    953 runs   "
        "(   21.19 ms per token,    47.19 tokens per second)\n"
        "llama_perf_context_print:       total time =  124186.21 ms /  104743 tokens\n"
        "llama_perf_context_print: prompt eval time =     421.55 ms /    110 tokens "
        "(    3.83 ms per token,   260.94 tokens per second)\n"
        "llama_perf_context_print:        eval time =    3877.47 ms /    247 runs   "
        "(   15.76 ms per token,    63.44 tokens per second)\n"
        "llama_perf_context_print:       total time =    4299.02 ms /    357 tokens\n"
    )
    t = LlamaLogMetrics(p)
    snap = t.read()
    assert snap["requests"] == 2
    # Weighted prompt rate = (103790 + 110) / (104.01332 + 0.42155) s — close to
    # the big prompt's 997.85, NOT the arithmetic mean (629.40).
    assert snap["prompt_tokens_per_sec"] == pytest.approx(103900 / 104.43487, rel=1e-4)
    assert snap["prompt_tokens_per_sec"] > 900
    assert (997.85 + 260.94) / 2 < snap["prompt_tokens_per_sec"]  # not the mean
    # Weighted eval rate = (953 + 247) / (20.17289 + 3.87747) s.
    assert snap["generation_tokens_per_sec"] == pytest.approx(1200 / 24.05036, rel=1e-4)


def test_partial_line_is_buffered_until_complete(tmp_path):
    p = str(tmp_path / "a1.log")
    # first write ends mid-line (no trailing newline): nothing counted yet
    with open(p, "w") as f:
        f.write("llama_perf_context_print: total time =     691")
    t = LlamaLogMetrics(p)
    assert t.read()["total_tokens"] == 0
    # complete the line: now counted
    with open(p, "a") as f:
        f.write(".46 ms /   300 tokens\n")
    snap = t.read()
    assert snap["total_tokens"] == 300
    assert snap["requests"] == 1


def test_truncation_keeps_lifetime(tmp_path):
    p = str(tmp_path / "a1.log")
    _write(p, _T1)
    t = LlamaLogMetrics(p)
    assert t.read()["total_tokens"] == 300
    # the log was truncated / rewritten to a fresh session (app restart): the
    # lifetime is KEPT and the fresh session is read from the start (no
    # double-count of the new content).
    _write(p, "llama_perf_context_print:       total time =    100.00 ms /    10 tokens\n")
    snap = t.read()
    assert snap["total_tokens"] == 310  # 300 (kept) + 10 (fresh session)
    assert snap["requests"] == 2
    # re-read: idempotent (no double-count)
    assert t.read()["total_tokens"] == 310


def test_seed_resumes_after_offset(tmp_path):
    p = str(tmp_path / "a1.log")
    _write(p, _T1)
    t = LlamaLogMetrics(p)
    t.read()  # consume _T1
    persisted = t.state()
    # a new request appends to the intact log (same session)
    with open(p, "a") as f:
        f.write(_T2)
    # simulate a server restart: a fresh tailer seeded with the persisted state
    t2 = LlamaLogMetrics(p)
    t2.seed(persisted)
    snap = t2.read()
    assert snap["total_tokens"] == 450  # 300 (kept) + 150 (_T2)
    assert snap["requests"] == 2  # _T1 + _T2, no double-count


def test_seed_keeps_lifetime_on_truncation(tmp_path):
    p = str(tmp_path / "a1.log")
    _write(p, _T1)
    t = LlamaLogMetrics(p)
    t.read()
    persisted = t.state()
    # the app restarted: the log is truncated to a fresh session
    _write(p, "llama_perf_context_print:       total time =    100.00 ms /    10 tokens\n")
    t2 = LlamaLogMetrics(p)
    t2.seed(persisted)
    snap = t2.read()
    # lifetime kept (300) + fresh session (10) = 310
    assert snap["total_tokens"] == 310
    assert snap["requests"] == 2


def test_draft_and_lcp_parsing(tmp_path):
    p = str(tmp_path / "a1.log")
    with open(p, "w") as f:
        f.write(
            "llama_perf_context_print: prompt eval time =    1234.56 ms /   100 tokens "
            "(   12.35 ms per token,   81.00 tokens per second)\n"
            "draft acceptance = 0.40944 (  642 accepted /  1568 generated), mean len =  3.87\n"
            "I slot get_availabl: id  0 | task -1 | selected slot by LCP similarity, f_sim_best = 0.996\n"
        )
    t = LlamaLogMetrics(p)
    snap = t.read()
    assert snap["input_tokens"] == 100
    assert snap["draft_acceptance"] == round(642 / 1568 * 100.0, 1)  # percentage
    assert snap["cache_hit_pct"] == 100.0  # 1/1 slot selections used LCP
    assert snap["draft_mean_len"] > 0


def test_lru_slot_is_not_a_cache_hit(tmp_path):
    p = str(tmp_path / "a1.log")
    with open(p, "w") as f:
        f.write(
            "I slot get_availabl: id  0 | task -1 | selected slot by LRU, t_last = -1\n"
        )
    t = LlamaLogMetrics(p)
    snap = t.read()
    assert snap["cache_hit_pct"] == 0.0  # LRU slot: no prefix cache used
    assert t.state()["slot_selections"] == 1


def test_missing_log_is_stale(tmp_path):
    t = LlamaLogMetrics(str(tmp_path / "nope.log"))
    snap = t.read()
    assert snap["stale"] is True
    assert snap["total_tokens"] == 0


def test_eval_line_not_matched_by_prompt_regex(tmp_path):
    # the eval line has "eval time" but not "prompt eval time"; only the output
    # tokens are counted, not the input tokens.
    p = str(tmp_path / "a1.log")
    with open(p, "w") as f:
        f.write(
            "llama_perf_context_print:        eval time =    5678.90 ms /   200 runs   "
            "(   28.39 ms per token,   35.22 tokens per second)\n"
        )
    t = LlamaLogMetrics(p)
    snap = t.read()
    assert snap["input_tokens"] == 0
    assert snap["output_tokens"] == 200
