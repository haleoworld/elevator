"""Password auth + session helper.

Single-user app. The password lives in the DASHBOARD_PASSWORD env var; on
startup we argon2-hash it once and keep the hash in memory. Login attempts
verify against that hash (constant-time via passlib).

Session: signed cookie via Starlette's SessionMiddleware.
"""
from __future__ import annotations

import os

from fastapi import Request
from fastapi.responses import RedirectResponse
from passlib.hash import argon2

_HASH: str | None = None  # set by init_password()


def init_password() -> None:
    """Hash DASHBOARD_PASSWORD once at startup."""
    global _HASH
    pw = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not pw:
        raise RuntimeError(
            "DASHBOARD_PASSWORD not set. Edit .env and set a strong password."
        )
    _HASH = argon2.hash(pw)


def verify(password: str) -> bool:
    if _HASH is None:
        raise RuntimeError("init_password() not called")
    try:
        return argon2.verify(password, _HASH)
    except (ValueError, TypeError):
        return False


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("authed"))


def require_login(request: Request) -> RedirectResponse | None:
    """Use as: `redirect = require_login(request)\\nif redirect: return redirect`."""
    if not is_logged_in(request):
        # Respect the prefix set by uvicorn --root-path so unauthenticated
        # redirects work behind path-based proxies (Tailscale Serve etc.).
        prefix = request.scope.get("root_path", "")
        return RedirectResponse(url=f"{prefix}/login", status_code=303)
    return None
