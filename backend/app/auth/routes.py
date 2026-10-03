"""/api/v1/auth: phone OTP login, token refresh, logout, profile, self-service roles.

Web clients get httpOnly SameSite=Strict cookies. Native/API clients send
``X-Auth-Mode: token`` to receive both tokens in the response body instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth.deps import ACCESS_COOKIE, REFRESH_COOKIE, current_user
from app.auth.service import AuthError, TokenPair
from app.container import Container, get_container
from app.schemas import User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
SELF_SERVICE_ROLES = {"buyer", "owner"}  # agent/developer need KYC + org verification


class OtpRequest(BaseModel):
    phone: str = Field(min_length=8, max_length=20)


class OtpRequestResponse(BaseModel):
    phone_e164: str
    expires_in: int
    dev_code: str | None = None  # only with APT_DEV_OTP_IN_RESPONSE=true outside prod


class OtpVerify(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    code: str = Field(min_length=4, max_length=8)
    full_name: str | None = Field(default=None, max_length=120)


class SessionResponse(BaseModel):
    user: User
    access_expires_in: int
    access_token: str | None = None
    refresh_token: str | None = None


class RoleRequest(BaseModel):
    role: str


def _token_mode(request: Request) -> bool:
    return request.headers.get("x-auth-mode", "").lower() == "token"


def _session(request: Request, response: Response, pair: TokenPair, c: Container) -> SessionResponse:
    if _token_mode(request):
        return SessionResponse(user=pair.user, access_expires_in=pair.access_expires_in,
                               access_token=pair.access_token, refresh_token=pair.refresh_token)
    secure = c.settings.cookie_secure
    response.set_cookie(ACCESS_COOKIE, pair.access_token, max_age=pair.access_expires_in, httponly=True,
                        secure=secure, samesite="strict", path="/api")
    response.set_cookie(REFRESH_COOKIE, pair.refresh_token, max_age=pair.refresh_expires_in, httponly=True,
                        secure=secure, samesite="strict", path="/api/v1/auth")
    return SessionResponse(user=pair.user, access_expires_in=pair.access_expires_in)


def _clear(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/api")
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


@router.post("/otp/request", response_model=OtpRequestResponse)
async def otp_request(body: OtpRequest, c: Container = Depends(get_container)):
    try:
        phone, code = await c.auth.request_otp(body.phone)
    except AuthError as e:
        raise HTTPException(e.status, str(e)) from e
    return OtpRequestResponse(phone_e164=phone, expires_in=c.settings.otp_ttl_seconds,
                              dev_code=code if c.settings.dev_otp_in_response and c.settings.env != "prod" else None)


@router.post("/otp/verify", response_model=SessionResponse)
async def otp_verify(body: OtpVerify, request: Request, response: Response, c: Container = Depends(get_container)):
    try:
        pair = await c.auth.verify_otp(body.phone, body.code, body.full_name)
    except AuthError as e:
        raise HTTPException(e.status, str(e)) from e
    return _session(request, response, pair, c)


class RefreshBody(BaseModel):
    refresh_token: str | None = None


@router.post("/refresh", response_model=SessionResponse)
async def refresh(request: Request, response: Response, body: RefreshBody | None = None,
                  c: Container = Depends(get_container)):
    token = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE)
    try:
        pair = await c.auth.refresh(token or "")
    except AuthError as e:
        failed = JSONResponse({"detail": str(e)}, status_code=e.status)
        _clear(failed)  # a dead refresh token should not keep being replayed by the browser
        return failed
    return _session(request, response, pair, c)


@router.post("/logout", status_code=204)
async def logout(request: Request, body: RefreshBody | None = None, c: Container = Depends(get_container)):
    await c.auth.logout((body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE))
    out = Response(status_code=204)
    _clear(out)
    return out


@router.get("/me", response_model=User)
async def me(user: User = Depends(current_user)):
    return user


@router.post("/roles", response_model=User)
async def add_role(body: RoleRequest, user: User = Depends(current_user), c: Container = Depends(get_container)):
    if body.role not in SELF_SERVICE_ROLES:
        raise HTTPException(403, f"'{body.role}' accounts are enabled after KYC verification; contact support")
    return await c.db.add_role(user.id, body.role)
