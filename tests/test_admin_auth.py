"""Phase 5 — admin authentication (spec 12 §3).

Local admin accounts (scrypt, salted), session-based auth (httpOnly
SameSite=Strict cookie), CSRF protection on state-changing endpoints, and
explicit confirmation for destructive actions (Force Stop / Delete / Clear
Queue). Auth is optional in development: open until the first admin exists.
"""

from __future__ import annotations

import httpx
from conftest import ADMIN_PORT


async def test_admin_auth_flow(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"

    # -- bootstrap: no admin yet → creating one is open --------------------------
    r = await c.post(f"{admin}/api/auth/admins", json={"username": "admin", "password": "secret"})
    assert r.status_code == 200
    assert r.json()["username"] == "admin"
    # No plaintext password is ever returned; listing shows only the username.
    admins = (await c.get(f"{admin}/api/auth/admins")).json()["admins"]
    assert [a["username"] for a in admins] == ["admin"]
    assert "password" not in str(r.json())

    # -- read-only GETs stay open (loopback default) -----------------------------
    assert (await c.get(f"{admin}/api/status")).status_code == 200

    # -- once an admin exists, state-changing endpoints require a session --------
    assert (await c.post(f"{admin}/api/applications/a1/start")).status_code == 401
    # Wrong credentials are rejected.
    assert (
        await c.post(f"{admin}/api/auth/login", json={"username": "admin", "password": "wrong"})
    ).status_code == 401

    # -- login issues a session cookie + CSRF token ------------------------------
    r = await c.post(f"{admin}/api/auth/login", json={"username": "admin", "password": "secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin" and body["csrf"]
    assert "gslb_session" in c.cookies

    # -- /me reflects the authenticated user -------------------------------------
    assert (await c.get(f"{admin}/api/auth/me")).json()["username"] == "admin"

    # -- state-changing WITHOUT a CSRF token is rejected (403) -------------------
    r = await c.post(f"{admin}/api/applications/a1/start")
    assert r.status_code == 403

    # -- state-changing WITH session + CSRF succeeds -----------------------------
    r = await c.post(
        f"{admin}/api/applications/a1/start", headers={"X-CSRF-Token": body["csrf"]}
    )
    assert r.status_code == 202

    # -- destructive action requires explicit confirmation (spec 12 §3) ----------
    # Force Stop without confirm → 409.
    r = await c.post(
        f"{admin}/api/applications/a1/stop?force=true",
        headers={"X-CSRF-Token": body["csrf"]},
    )
    assert r.status_code == 409
    # With confirm=true it proceeds.
    r = await c.post(
        f"{admin}/api/applications/a1/stop?force=true&confirm=true",
        headers={"X-CSRF-Token": body["csrf"]},
    )
    assert r.status_code == 202

    # -- Clear Queue also requires confirmation ----------------------------------
    r = await c.post(f"{admin}/api/gateways/gw-a1/clear-queue", headers={"X-CSRF-Token": body["csrf"]})
    assert r.status_code == 409
    r = await c.post(
        f"{admin}/api/gateways/gw-a1/clear-queue?confirm=true",
        headers={"X-CSRF-Token": body["csrf"]},
    )
    assert r.status_code == 200

    # -- logout revokes the session ----------------------------------------------
    await c.post(f"{admin}/api/auth/logout")
    assert (await c.get(f"{admin}/api/auth/me")).status_code == 401
    # A new client with no session is rejected on mutating endpoints.
    c2 = httpx.AsyncClient()
    assert (await c2.post(f"{admin}/api/applications/a1/start")).status_code == 401
    await c2.aclose()
    await c.aclose()


def test_session_store_is_bounded():
    """The in-memory session store is bounded (spec 12 §3): issuing more than
    _MAX_SESSIONS sessions evicts the oldest rather than growing without limit."""
    import gpusybio.auth.admin as admin_mod
    from gpusybio.auth.admin import create_session, validate_session

    saved = dict(admin_mod._SESSIONS)
    admin_mod._SESSIONS.clear()
    try:
        cap = admin_mod._MAX_SESSIONS
        # create_session returns (token, csrf) — keep the token.
        tokens = [token for token, _ in (create_session(f"user{i}") for i in range(cap + 5))]
        # The store never exceeds the cap.
        assert len(admin_mod._SESSIONS) == cap
        # The oldest issued session was evicted; the newest is valid.
        assert validate_session(tokens[0]) is None
        assert validate_session(tokens[-1]) is not None
    finally:
        admin_mod._SESSIONS.clear()
        admin_mod._SESSIONS.update(saved)


async def test_admin_password_hashed(server):
    """The stored password is salted + hashed (scrypt), never plaintext."""
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    await c.post(f"{admin}/api/auth/admins", json={"username": "admin", "password": "secret"})
    from gpusybio.auth.admin import verify_password
    from gpusybio.database.models import AdminUser

    with server.db() as s:
        row = s.query(AdminUser).filter_by(username="admin").first()
    assert row is not None
    # Hash format is scrypt$<salt>$<hash>, and it is NOT the plaintext.
    assert row.password_hash.startswith("scrypt$")
    assert "secret" not in row.password_hash
    assert verify_password("secret", row.password_hash) is True
    assert verify_password("nope", row.password_hash) is False
    await c.aclose()
