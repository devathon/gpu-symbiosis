"""Unit: the llama.cpp log tailer (spec 08 §5).

The application's stdout is the log; the metrics handler tails it for the
per-request token lines (``llama_perf_context_print``) and accumulates the
lifetime totals. These tests exercise the tailer directly (offset tracking,
cumulative totals, the average tokens-per-second, and rotation safety).
"""

from __future__ import annotations

import os

from gpusybio.metrics.llama_log import LlamaLogMetrics


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
    # average prompt / generation tokens-per-second (mean over the requests)
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


def test_rotation_resets_lifetime(tmp_path):
    p = str(tmp_path / "a1.log")
    _write(p, _T1)
    t = LlamaLogMetrics(p)
    assert t.read()["total_tokens"] == 300
    # the log shrank (rotated / truncated to a fresh, shorter file): reset and
    # re-read from the top (a single request line, well under the prior size).
    _write(p, "llama_perf_context_print:       total time =    100.00 ms /    10 tokens\n")
    snap = t.read()
    assert snap["input_tokens"] == 0
    assert snap["total_tokens"] == 10
    assert snap["requests"] == 1


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
