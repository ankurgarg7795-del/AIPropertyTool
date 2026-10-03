"""FastAPI dependencies: resolve the caller from a Bearer header or the access cookie."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.auth.service import AuthError
from app.container import Container, get_container
from app.schemas import User

ACCESS_COOKIE = "apt_at"
REFRESH_COOKIE = "apt_rt"


def _token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(ACCESS_COOKIE)


async def optional_user(request: Request, c: Container = Depends(get_container)) -> User | None:
    """Anonymous callers get None. A present-but-invalid/expired token is a 401 so
    the client refreshes instead of silently continuing as a guest."""
    token = _token(request)
    if not token:
        return None
    try:
        claims = c.auth.decode_access(token)
    except AuthError as e:
        raise HTTPException(e.status, str(e), headers={"WWW-Authenticate": "Bearer"}) from e
    user = await c.db.get_user(claims["sub"])
    if not user:
        raise HTTPException(401, "Not signed in", headers={"WWW-Authenticate": "Bearer"})
    return user


async def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(401, "Sign in required", headers={"WWW-Authenticate": "Bearer"})
    return user


def require_role(*roles: str):
    async def dep(user: User = Depends(current_user)) -> User:
        if not set(roles) & set(user.roles):
            raise HTTPException(403, f"Requires role: {' or '.join(roles)}")
        return user

    return dep
