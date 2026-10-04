"""The ``shell-command`` application type: per-request command runner.

Unit coverage of the substitution contract and the command runner; an
integration test of the command server's HTTP surface (health + JSON/raw
responses, injection resistance, missing params, timeouts); the type
registration; and the env channel by which the app's config reaches the server
(``extra_env`` -> ``GPUSYB_COMMAND_*``). The full framework loop (start on
demand, VRAM check, forward, idle-unload) is covered by the e2e test in
``test_e2e_shell_command``.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gpusybio.command_server import _run_command, _substitute, build_app
from gpusybio.config.models import ApplicationSpec
from gpusybio.config.seed import _apply_app_config
from gpusybio.database.models import Application
from gpusybio.applications.manager import _parse_command_env
from gpusybio.applications.scripts import run_start_script
from gpusybio.metrics.handlers import GenericHandler
from gpusybio.metrics.type_registry import make_type_registry


# -- substitution contract -----------------------------------------------------

def test_substitute_named_param():
    assert _substitute("echo $name", "?name=hello") == "echo hello"


def test_substitute_quotes_special_chars_to_one_token():
    # A malicious value must become a single inert token (no injection).
    assert _substitute("echo $name", "?name=; rm -rf ~") == "echo '; rm -rf ~'"


def test_substitute_quotes_whitespace_to_one_token():
    # No word-splitting: a space-containing value is one quoted token.
    assert _substitute("echo $name", "?name=foo bar") == "echo 'foo bar'"


def test_substitute_params_catch_all():
    # All values, in order, each quoted, space-joined (the ffmpeg case).
    got = _substitute(
        "ffmpeg $params -v 1",
        "?params=-i&params=audio/raju2.mp3&params=audio/raju2.wav",
    )
    assert got == "ffmpeg -i audio/raju2.mp3 audio/raju2.wav -v 1"


def test_substitute_url_decodes_values():
    assert _substitute("echo $name", "?name=a%20b") == "echo 'a b'"


def test_substitute_named_and_params_together():
    # $name -> the named value; $params -> every value (name included).
    assert _substitute("$name $params", "?name=x&y=1&y=2") == "x x 1 2"


def test_substitute_literal_dollar_not_identifier_stays():
    assert _substitute("cost $5 and $6", "?name=x") == "cost $5 and $6"


def test_substitute_tolerates_leading_question_mark():
    assert _substitute("echo $name", "name=hello") == "echo hello"
    assert _substitute("echo $name", "?name=hello") == "echo hello"


def test_substitute_missing_param_raises_lookuperror():
    with pytest.raises(LookupError):
        _substitute("echo $missing", "?name=x")


def test_substitute_params_with_no_query_raises_valueerror():
    with pytest.raises(ValueError):
        _substitute("ffmpeg $params", "")


# -- command runner -------------------------------------------------------------

async def _run(cmd: str, **kw):
    return await _run_command(cmd, kw.get("cwd"), kw.get("env", {}), kw.get("timeout", 10.0))


async def test_run_captures_stdout_and_exit():
    r = await _run("echo hello")
    assert r["exit_code"] == 0
    assert r["stdout"].startswith("hello")
    assert r["timed_out"] is False
    assert r["truncated"] is False


async def test_run_nonzero_exit_is_completion_not_error():
    r = await _run("echo oops; exit 3")
    assert r["exit_code"] == 3
    assert r["timed_out"] is False
    assert r["stdout"].startswith("oops")


async def test_run_captures_stderr():
    r = await _run("echo to-err 1>&2")
    assert r["exit_code"] == 0
    assert "to-err" in r["stderr"]


async def test_run_timeout_marks_timed_out():
    r = await _run("sleep 5", timeout=0.5)
    assert r["timed_out"] is True
    assert r["exit_code"] != 0


async def test_run_spawn_failure_is_code_127():
    r = await _run("definitely-not-a-real-command-xyz")
    assert r["exit_code"] == 127
    assert r["timed_out"] is False


async def test_run_truncates_oversized_output():
    # 1 MiB cap per stream.
    r = await _run(f"python3 -c \"import sys; sys.stdout.write('A'*{2*1024*1024})\"")
    assert r["truncated"] is True
    assert len(r["stdout"]) == 1024 * 1024


async def test_run_uses_cwd():
    r = await _run("pwd", cwd="/tmp")
    assert r["stdout"].strip() == "/tmp"


async def test_run_uses_extra_env():
    r = await _run("echo $MY_SECRET", env={"PATH": os.environ["PATH"], "MY_SECRET": "s3cret"})
    assert r["stdout"].strip() == "s3cret"


# -- type registration ----------------------------------------------------------

def test_shell_command_type_registered():
    reg = make_type_registry()
    assert "shell-command" in reg.known()
    spec = reg.spec_for("shell-command", None)
    assert spec.type == "shell-command"
    assert [m.id for m in spec.metrics] == ["requests", "avg_latency_sec"]
    assert isinstance(reg.handler_for("shell-command", None, None, None, {}), GenericHandler)
    # every request is scheduled (they all do work)
    assert spec.should_schedule("GET", "/anything") is True
    assert spec.should_schedule("POST", "/x?name=y") is True


# -- env channel (extra_env -> GPUSYB_COMMAND_*) --------------------------------

async def test_extra_env_reaches_child_process(tmp_path):
    """The start path passes the app's config to the server via env; a child
    process sees GPUSYB_COMMAND_* set from extra_env."""
    log = str(tmp_path / "e.log")
    pid = str(tmp_path / "e.pid")
    r = await run_start_script(
        'exec env | grep -o "GPUSYB_COMMAND_TEMPLATE=.*" ',
        app_id="cmd1", log_path=log, pid_path=pid, grace=2.0,
        extra_env={"GPUSYB_COMMAND_TEMPLATE": "echo hi"},
    )
    assert r.error is None
    # The child printed its own env value, proving the channel works.
    with open(log) as f:
        assert "GPUSYB_COMMAND_TEMPLATE=echo hi" in f.read()


# -- config mapping (spec -> row) ------------------------------------------------

def test_command_fields_map_to_row():
    row = Application(id="c1", name="c1")
    spec = ApplicationSpec(
        id="c1", name="c1", port=1234, gpu_memory_mb=1024,
        application_type="shell-command",
        start_script="ffmpeg $params -v 1",
        command_cwd="/srv/media",
        command_env={"KEY": "value"},
        command_response_mode="raw",
        command_timeout_sec=90.0,
    )
    _apply_app_config(row, spec)
    assert row.start_script == "ffmpeg $params -v 1"
    assert row.command_cwd == "/srv/media"
    assert row.command_env == '{"KEY": "value"}'  # stored as a JSON document
    assert row.command_response_mode == "raw"
    assert row.command_timeout_sec == 90.0


def test_command_env_parse_helper():
    assert _parse_command_env('{"A": "b"}') == {"A": "b"}
    assert _parse_command_env(None) is None
    assert _parse_command_env("not-json") is None
    assert _parse_command_env('[1, 2]') is None  # not an object


# -- HTTP surface (integration) ---------------------------------------------------

async def _client(cfg: dict) -> TestClient:
    client = TestClient(TestServer(build_app(cfg)))
    await client.start_server()
    return client


async def test_health_endpoint():
    cfg = {"template": "echo $name", "cwd": None, "env": {}, "response_mode": "json",
           "timeout": 10.0}
    client = await _client(cfg)
    r = await client.get("/health")
    assert r.status == 200
    await client.close()


async def test_non_post_is_405():
    """The command runs on POST only: any other method is rejected with 405
    (without running the command) — the health probe is the only GET surface."""
    cfg = {"template": "echo $name", "cwd": None, "env": {}, "response_mode": "json",
           "timeout": 10.0}
    client = await _client(cfg)
    r = await client.get("/?name=hello")
    assert r.status == 405
    assert r.headers.get("Allow") == "POST"
    # A PUT is likewise rejected.
    r = await client.put("/?name=hello")
    assert r.status == 405
    await client.close()


async def test_json_response_completed():
    cfg = {"template": "echo $name", "cwd": None, "env": {}, "response_mode": "json",
           "timeout": 10.0}
    client = await _client(cfg)
    r = await client.post("/?name=hello")
    assert r.status == 200
    data = await r.json()
    assert data["exit_code"] == 0
    assert data["stdout"].startswith("hello")
    assert data["timed_out"] is False
    await client.close()


async def test_nonzero_exit_is_200_with_exit_code():
    cfg = {"template": "exit 7", "cwd": None, "env": {}, "response_mode": "json",
           "timeout": 10.0}
    client = await _client(cfg)
    r = await client.post("/")
    assert r.status == 200  # completion, not an HTTP error
    data = await r.json()
    assert data["exit_code"] == 7
    await client.close()


async def test_missing_param_is_400():
    cfg = {"template": "echo $name", "cwd": None, "env": {}, "response_mode": "json",
           "timeout": 10.0}
    client = await _client(cfg)
    r = await client.post("/")
    assert r.status == 400
    data = await r.json()
    assert "missing param" in data["error"]
    await client.close()


async def test_injection_neutralized_to_one_token():
    cfg = {"template": "echo $name", "cwd": None, "env": {}, "response_mode": "json",
           "timeout": 10.0}
    client = await _client(cfg)
    r = await client.post("/?name=hello%20world%3B%20echo%20pwned")
    data = await r.json()
    # The value is one quoted token; the injected command did not run.
    assert "hello world; echo pwned" in data["stdout"]
    assert "pwned\n" not in data["stdout"].split("hello world; echo pwned")[-1]
    await client.close()


async def test_raw_mode_returns_stdout_verbatim():
    cfg = {"template": "echo $name", "cwd": None, "env": {}, "response_mode": "raw",
           "timeout": 10.0}
    client = await _client(cfg)
    r = await client.post("/?name=raw-hello")
    assert r.status == 200
    assert (await r.text()).strip() == "raw-hello"
    await client.close()


async def test_timeout_is_504():
    cfg = {"template": "sleep 3", "cwd": None, "env": {}, "response_mode": "json",
           "timeout": 0.4}
    client = await _client(cfg)
    r = await client.post("/")
    assert r.status == 504
    data = await r.json()
    assert data["timed_out"] is True
    await client.close()
