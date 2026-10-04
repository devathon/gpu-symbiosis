"""The built-in command server (``shell-command`` application type).

Launched by the framework's start path as ``python -m gpusybio.command_server``
(its config reaches it via ``GPUSYB_COMMAND_*`` env, which the start path sets
from the application row). It listens on ``GPUSYB_PORT`` and, for every POST
request, runs ``GPUSYB_COMMAND_TEMPLATE`` with the request's URL query parameters
substituted in, and returns the result (any other method is rejected with 405;
``/health`` is the readiness probe).

Parameter substitution (the contract):
- ``$name``  -> the URL-decoded value of query param ``name``, wrapped in
  ``shlex.quote`` (exactly ONE token - no shell injection, no word-splitting).
- ``$params`` (catch-all) -> ALL query param values, in order of first
  appearance, each quoted, joined by a single space. For passing a whole
  argument blob: ``ffmpeg $params -v 1`` with ``?a=-i&b=audio/raju2.mp3&c=audio/raju2.wav``.
- A referenced ``$name`` that is absent from the query -> ``400``.

Only param *values* are substituted and each is quoted, so the template's own
structure is operator-trusted and client input can never inject a command.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
import urllib.parse

from aiohttp import web

CFG_KEY = web.AppKey("cfg", dict)
DEFAULT_TIMEOUT_SEC = 300.0
OUTPUT_CAP_BYTES = 1024 * 1024  # 1 MiB per stream

# ``$ident``: a dollar followed by a Python-identifier (the param name).
_NAME_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _decode_query(query: str) -> list[tuple[str, str]]:
    """Ordered ``(name, url-decoded value)`` pairs from a raw query string.

    Tolerates a leading ``?`` (aiohttp's ``request.query_string`` omits it)."""
    if not query:
        return []
    if query.startswith("?"):
        query = query[1:]
    if not query:
        return []
    return list(urllib.parse.parse_qsl(query, keep_blank_values=True))


def _substitute(template: str, query: str) -> str:
    """Substitute ``$name`` and the ``$params`` catch-all into ``template``.

    Raises ``LookupError`` naming a missing ``$name``, and ``ValueError`` when
    ``$params`` is referenced but the query has no parameters.
    """
    pairs = _decode_query(query)
    by_name: dict[str, list[str]] = {}
    for k, v in pairs:
        by_name.setdefault(k, []).append(v)

    result = template
    if "$params" in result:
        if not pairs:
            raise ValueError("$params requires at least one query parameter")
        result = result.replace("$params", " ".join(shlex.quote(v) for _, v in pairs))

    def _repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in by_name:
            raise LookupError(name)
        return shlex.quote(by_name[name][0])  # first value for a repeated name

    return _NAME_RE.sub(_repl, result)


async def _run_command(command: str, cwd: str | None, env: dict[str, str], timeout: float) -> dict:
    """Run ``command`` (POSIX shell), capturing stdout/stderr (capped)."""
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as e:
        return {
            "exit_code": 127, "stdout": "", "stderr": f"spawn failed: {e}",
            "duration_sec": 0.0, "timed_out": False, "truncated": False,
        }
    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            stdout_b, stderr_b = b"", b""
    out = stdout_b.decode("utf-8", "replace")
    err = stderr_b.decode("utf-8", "replace")
    truncated = len(out) > OUTPUT_CAP_BYTES or len(err) > OUTPUT_CAP_BYTES
    return {
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "stdout": out[:OUTPUT_CAP_BYTES],
        "stderr": err[:OUTPUT_CAP_BYTES],
        "duration_sec": round(time.monotonic() - t0, 3),
        "timed_out": timed_out,
        "truncated": truncated,
    }


def _http_status(result: dict) -> int:
    """HTTP status for a completed command run (spec: 200 completed, 504 timeout,
    500 spawn failure). The command's own exit code travels in the body."""
    if result["timed_out"]:
        return 504
    if result["exit_code"] == 127:
        return 500
    return 200


async def _handle(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    # The health probe (the readiness check) never runs a command.
    if request.path == "/health":
        return web.Response(status=200, text="" if request.method == "HEAD" else "ok")
    # The command runs on POST only (it is a side-effecting request); any other
    # method is rejected with 405 (the health probe is the only GET/HEAD surface).
    if request.method != "POST":
        return web.Response(
            status=405, text="Method Not Allowed: run the command with POST",
            headers={"Allow": "POST"},
        )
    try:
        command = _substitute(cfg["template"], request.query_string)
    except LookupError as e:
        return web.json_response({"error": f"missing param: {e.args[0]}"}, status=400)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    result = await _run_command(command, cfg["cwd"], cfg["env"], cfg["timeout"])
    status = _http_status(result)
    if cfg["response_mode"] == "raw":
        return web.Response(status=status, text=result["stdout"])
    return web.json_response(result, status=status)


def _config_from_env() -> dict:
    raw_env = os.environ.get("GPUSYB_COMMAND_ENV")
    try:
        env = json.loads(raw_env) if raw_env else {}
        if not isinstance(env, dict):
            env = {}
    except (json.JSONDecodeError, ValueError):
        env = {}
    return {
        "port": int(os.environ.get("GPUSYB_PORT") or 0),
        "template": os.environ.get("GPUSYB_COMMAND_TEMPLATE", ""),
        # Default the working dir to the data path so relative paths resolve
        # predictably; an explicit GPUSYB_COMMAND_CWD wins.
        "cwd": os.environ.get("GPUSYB_COMMAND_CWD") or os.environ.get("GPUSYB_DATA_PATH") or None,
        "env": env,
        "response_mode": os.environ.get("GPUSYB_COMMAND_RESPONSE_MODE", "json"),
        "timeout": (
            float(os.environ["GPUSYB_COMMAND_TIMEOUT_SEC"])
            if os.environ.get("GPUSYB_COMMAND_TIMEOUT_SEC")
            else DEFAULT_TIMEOUT_SEC
        ),
        "pid_file": os.environ.get("GPUSYB_PID_FILE"),
    }


def _write_pid_file(path: str | None) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"{os.getpid()}\n")
    except OSError:
        pass


def build_app(cfg: dict) -> web.Application:
    """The command server's aiohttp app (health probe + a catch-all that runs the
    command on POST). Factored out so tests reuse the exact wiring (no ``run_app``)."""
    app = web.Application()
    app[CFG_KEY] = cfg
    # A single catch-all route (any method, any path): ``/health`` is the health
    # probe; POST runs the command; any other method is 405. (Branching in the
    # handler avoids aiohttp's duplicate-method registration error.)
    app.router.add_route("*", "/{tail:.*}", _handle)
    return app


def main() -> None:
    cfg = _config_from_env()
    if not cfg["template"]:
        raise SystemExit("GPUSYB_COMMAND_TEMPLATE is required")
    if not cfg["port"]:
        raise SystemExit("GPUSYB_PORT is required")
    _write_pid_file(cfg["pid_file"])
    web.run_app(build_app(cfg), host="127.0.0.1", port=cfg["port"])


if __name__ == "__main__":
    main()
