"""Admin auth routes (spec 12 §3) + the admin session/CSRF middleware.

``register`` binds the shared names from the :class:`ApiContext` at the top;
the route bodies below are unchanged from the original ``make_admin_api``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from gpusybio.auth.admin import (
    admin_configured,
    check_csrf,
    create_admin,
    create_session,
    delete_admin,
    list_admins,
    revoke_session,
    verify_admin,
)
from gpusybio.api.views import AuthBody

# State-changing admin endpoints require a valid session (+ CSRF) once any
# admin account exists; read-only GETs stay open (loopback default).
_AUTH_EXEMPT = {"/api/auth/login", "/api/auth/logout", "/api/auth/admins"}
_MUTATING = {"POST", "PUT", "DELETE", "PATCH"}


def register(api, ctx) -> None:
    db = ctx.db
    _audit = ctx.audit
    _actor = ctx.actor
    _session_from_scope = ctx.session_from_scope
    _session_from_request = ctx.session_from_request
    _csrf_from_request = ctx.csrf_from_request
    _cookie_params = ctx.cookie_params

    @api.middleware("http")
    async def _admin_auth(request, call_next):
        if request.method in _MUTATING and request.url.path.startswith("/api/"):
            if request.url.path not in _AUTH_EXEMPT and admin_configured(db):
                session = _session_from_scope(request.scope)
                if session is None:
                    return JSONResponse({"error": "authentication required"}, status_code=401)
                if not check_csrf(session, _csrf_from_request(request)):
                    return JSONResponse({"error": "invalid or missing CSRF token"}, status_code=403)
        return await call_next(request)

    @api.post("/api/auth/login")
    async def login(body: AuthBody, response: Response):
        admin_id = verify_admin(db, body.username, body.password)
        if admin_id is None:
            raise HTTPException(401, "invalid credentials")
        token, csrf = create_session(body.username)
        response.set_cookie(**_cookie_params(token))
        await _audit("admin_login", actor=body.username)
        return {"username": body.username, "csrf": csrf, "token": token}

    @api.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        session = _session_from_request(request)
        if session is not None:
            revoke_session(request.cookies.get("gslb_session"))
        response.delete_cookie("gslb_session", path="/")
        return {"ok": True}

    @api.get("/api/auth/me")
    async def me(request: Request):
        session = _session_from_request(request)
        if session is None:
            raise HTTPException(401, "not authenticated")
        return {"username": session["username"]}

    @api.get("/api/auth/csrf")
    async def csrf_token(request: Request):
        # Returns the session's CSRF token so the UI can re-acquire it after a
        # page reload (the double-submit token is session-scoped, spec 12 §3).
        session = _session_from_request(request)
        if session is None:
            raise HTTPException(401, "not authenticated")
        return {"csrf": session["csrf"]}

    @api.post("/api/auth/admins")
    async def create_admin_endpoint(body: AuthBody, request: Request):
        # Bootstrap: open when no admin exists; otherwise requires a session.
        if admin_configured(db) and _session_from_request(request) is None:
            raise HTTPException(401, "authentication required")
        try:
            admin_id, username = create_admin(db, body.username, body.password)
        except ValueError as e:
            raise HTTPException(409, str(e))
        await _audit("admin_created", actor=body.username, detail=f"admin {username}")
        return {"id": admin_id, "username": username}

    @api.get("/api/auth/admins")
    async def list_admins_endpoint():
        # Open (usernames only): lets the UI detect whether to show the login
        # screen or the first-admin bootstrap screen. No passwords exposed.
        return {"admins": list_admins(db)}

    @api.delete("/api/auth/admins/{admin_id}")
    async def delete_admin_endpoint(admin_id: str, request: Request):
        session = _session_from_request(request)
        if session is None:
            raise HTTPException(401, "authentication required")
        if not delete_admin(db, admin_id):
            raise HTTPException(404, "not found")
        await _audit("admin_deleted", actor=session["username"], detail=admin_id)
        return {"ok": True}
