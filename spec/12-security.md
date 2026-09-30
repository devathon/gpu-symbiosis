# Module 12 — Security

The system exposes arbitrary application APIs and can execute scripts — it is
effectively administrative access to the machine. Security is therefore layered.

## 1. Network Binding

Because the gateway exposes arbitrary application APIs:

```text
bind address
authentication
API keys
allowed networks
```

**Default bind address: `127.0.0.1`** (loopback) rather than `0.0.0.0`, unless
explicitly configured. Same default for the management API and Web UI.

Per-gateway `allowed_networks` (CIDR list) may further restrict who may reach a
gateway port when it is bound non-locally.

## 2. Gateway Authentication

Optional per-gateway API keys:

- client sends `Authorization: Bearer <key>` (or a configurable header)
- invalid key → `401`
- keys stored hashed (SHA-256) in the database; UI shows them once at creation
- keys are checked **before** queueing (rejected requests never enter a queue)

The application's own auth (if any) still applies on top — the proxy passes
authentication headers through unchanged.

## 3. Web UI Security

At minimum: **admin authentication**, because the UI can:

```text
start processes
stop processes
execute scripts
```

Requirements:

- local admin account(s), password hashed (scrypt/argon2), salted
- session-based auth (httpOnly, SameSite=Strict cookies) or bearer tokens
- CSRF protection on all state-changing endpoints
- all destructive actions (Force Stop, Delete, Clear Queue) require explicit
  confirmation in the UI and are audited in the event log

## 4. Script Security

Scripts run with a controlled environment:

```text
configured working directory
configured environment variables
timeout
stdout/stderr capture
exit code
```

The script inherits the **service (orchestrator) environment** — start scripts
rely on `PATH`, `HOME`, `LD_LIBRARY_PATH`, etc. to find the application binary —
with the `GPUSYB_*` variables and any configured extra env layered on top.
"Controlled" refers to the working directory, timeout, output capture, exit code,
and the executing user — not to a sanitized environment.

Additional requirements:

- scripts run as a configured user (default: the service user, not root)
- no privileged capability escalation; the service user needs only what the
  application processes need
- environment injected: `GPUSYB_APP_ID`, `GPUSYB_PORT`, `GPUSYB_PID`
  (stop/load/unload), `GPUSYB_DATA_PATH`, `GPUSYB_LOG` and `GPUSYB_PID_FILE`
  (the effective log/pid paths, spec 03 §2.5)
- **record script failures**: exit code, timed-out flag, last N lines of
  stdout/stderr — stored on the event and shown in the UI

## 5. Audit

Every authenticated action (manual start/stop/load/unload, config change, key
creation, deletion) is written to the event log with the acting identity. The
event log is append-only from the application's perspective.
