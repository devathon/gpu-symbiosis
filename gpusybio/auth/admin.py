"""Admin authentication (spec 12 §3).

Local admin accounts with salted + hashed passwords (scrypt). Session-based
auth: a login issues an httpOnly SameSite=Strict session cookie plus a CSRF
token; state-changing endpoints require a valid session AND a matching
``X-CSRF-Token`` header (double-submit).

Auth is optional in development: when no admin account exists the admin API is
open (mirroring the per-gateway key model). Once any admin exists, all
state-changing endpoints require a valid session.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid

from gpusybio.database.db import SessionFactory
from gpusybio.database.models import AdminUser

# In-memory session store: token -> {username, csrf, created}.
# Process-local (single process, spec 02 §1) — sessions don't survive restarts.
_SESSIONS: dict[str, dict] = {}

SESSION_TTL_SEC = 12 * 3600  # 12 h
# Bound the store so a churn of logins (or a client minting sessions) can't grow
# it without limit: when full, the oldest session is evicted on the next issue.
_MAX_SESSIONS = 64


def _make_room() -> None:
    """Evict the oldest sessions (by created time) until there is room for one
    more. Called from create_session so the store stays bounded."""
    while len(_SESSIONS) >= _MAX_SESSIONS:
        oldest = min(_SESSIONS, key=lambda t: _SESSIONS[t]["created"])
        _SESSIONS.pop(oldest, None)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    """scrypt hash with a random salt. Format: ``scrypt$<salt_hex>$<hash_hex>``."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$", 2)
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def create_admin(db: SessionFactory, username: str, password: str) -> tuple[str, str]:
    """Create an admin. Returns (admin_id, username)."""
    admin_id = "admin-" + uuid.uuid4().hex[:12]
    with db() as s:
        existing = s.query(AdminUser).filter_by(username=username).first()
        if existing is not None:
            raise ValueError(f"username {username!r} already exists")
        s.add(AdminUser(id=admin_id, username=username, password_hash=hash_password(password)))
    return admin_id, username


def list_admins(db: SessionFactory) -> list[dict]:
    out = []
    with db() as s:
        for row in s.query(AdminUser).order_by(AdminUser.created_at):
            out.append(
                {
                    "id": row.id,
                    "username": row.username,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
            )
    return out


def delete_admin(db: SessionFactory, admin_id: str) -> bool:
    with db() as s:
        row = s.get(AdminUser, admin_id)
        if row is None:
            return False
        s.delete(row)
    return True


def admin_configured(db: SessionFactory) -> bool:
    with db() as s:
        return s.query(AdminUser).count() > 0


def verify_admin(db: SessionFactory, username: str, password: str) -> str | None:
    """Return the admin id if the credentials are valid, else None."""
    with db() as s:
        row = s.query(AdminUser).filter_by(username=username).first()
    if row is None:
        return None
    if verify_password(password, row.password_hash):
        return row.id
    return None


# -- sessions + CSRF -----------------------------------------------------------
def create_session(username: str) -> tuple[str, str]:
    """Issue a session token + CSRF token. Returns (session_token, csrf_token)."""
    token = _new_token()
    csrf = _new_token()
    _make_room()
    _SESSIONS[token] = {"username": username, "csrf": csrf, "created": time.time()}
    return token, csrf


def validate_session(token: str | None) -> dict | None:
    """Return the session record if the token is valid and unexpired, else None."""
    if not token:
        return None
    rec = _SESSIONS.get(token)
    if rec is None:
        return None
    if time.time() - rec["created"] > SESSION_TTL_SEC:
        _SESSIONS.pop(token, None)
        return None
    return rec


def revoke_session(token: str | None) -> None:
    if token:
        _SESSIONS.pop(token, None)


def check_csrf(session: dict, presented: str | None) -> bool:
    """Double-submit CSRF check: header token must match the session's token."""
    if not presented:
        return False
    return secrets.compare_digest(presented, session["csrf"])


def current_user(token: str | None) -> str | None:
    rec = validate_session(token)
    return rec["username"] if rec else None
