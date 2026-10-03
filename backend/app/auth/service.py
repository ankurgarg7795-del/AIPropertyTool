"""Phone-OTP authentication with short-lived JWT access tokens and rotating,
reuse-detecting refresh tokens.

* OTP: 6 digits, stored only as HMAC(secret, phone|code), 5-minute TTL,
  at most 5 sends per phone per hour and 5 verify attempts per code.
* Access token: HS256 JWT (15 min) with ``sub`` and ``roles``.
* Refresh token: 256-bit opaque value, stored as SHA-256, 30-day TTL, one-time
  use. Presenting an already-rotated token revokes its whole family (theft
  signal), forcing a fresh login on every device holding that chain.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

import jwt

from app.core.config import Settings
from app.db.base import Database, OtpRecord, RefreshRecord
from app.schemas import User

log = logging.getLogger(__name__)
ISSUER = "aipropertytool"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def normalize_phone(raw: str) -> str:
    """Return E.164. Bare 10-digit Indian mobiles get +91."""
    digits = re.sub(r"[\s\-().]", "", raw or "")
    if re.fullmatch(r"[6-9]\d{9}", digits):
        return "+91" + digits
    if re.fullmatch(r"0[6-9]\d{9}", digits):
        return "+91" + digits[1:]
    if re.fullmatch(r"91[6-9]\d{9}", digits):
        return "+" + digits
    if re.fullmatch(r"\+[1-9]\d{7,14}", digits):
        return digits
    raise AuthError("Enter a valid mobile number", 422)


class SmsSender(Protocol):
    async def send(self, phone_e164: str, text: str) -> None: ...


class LogSmsSender:
    """Dev/test sender. Production plugs in a DLT-registered provider (MSG91, Gupshup, Twilio)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, phone_e164: str, text: str) -> None:
        self.sent.append((phone_e164, text))
        log.info("SMS queued to %s****%s", phone_e164[:5], phone_e164[-2:])  # never log the code


@dataclass
class TokenPair:
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_in: int
    user: User


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class AuthService:
    def __init__(self, db: Database, settings: Settings, sms: SmsSender | None = None):
        self.db, self.settings = db, settings
        self.sms: SmsSender = sms or LogSmsSender()

    def _otp_hash(self, phone: str, code: str) -> str:
        return hmac.new(self.settings.jwt_secret.encode(), f"{phone}|{code}".encode(), hashlib.sha256).hexdigest()

    # ---- OTP ------------------------------------------------------------ #
    async def request_otp(self, raw_phone: str) -> tuple[str, str]:
        """Send a code. Returns (phone_e164, code); callers expose the code only in dev."""
        phone = normalize_phone(raw_phone)
        now = _now()
        prev = await self.db.get_otp(phone)
        window_start, sent = now, 1
        if prev and now - prev.window_start < timedelta(hours=1):
            if prev.sent_count >= self.settings.otp_max_sends_per_hour:
                raise AuthError("Too many codes requested. Try again later.", 429)
            window_start, sent = prev.window_start, prev.sent_count + 1
        code = f"{secrets.randbelow(10**6):06d}"
        await self.db.put_otp(OtpRecord(phone_e164=phone, code_hash=self._otp_hash(phone, code),
                                        expires_at=now + timedelta(seconds=self.settings.otp_ttl_seconds),
                                        attempts=0, sent_count=sent, window_start=window_start))
        await self.sms.send(phone, f"{code} is your AIPropertyTool login code. Valid for 5 minutes. Do not share it.")
        return phone, code

    async def verify_otp(self, raw_phone: str, code: str, full_name: str | None = None) -> TokenPair:
        phone = normalize_phone(raw_phone)
        rec = await self.db.get_otp(phone)
        if not rec or rec.expires_at < _now():
            raise AuthError("Code expired. Request a new one.")
        if rec.attempts >= self.settings.otp_max_attempts:
            await self.db.delete_otp(phone)
            raise AuthError("Too many attempts. Request a new code.", 429)
        if not hmac.compare_digest(rec.code_hash, self._otp_hash(phone, (code or "").strip())):
            rec.attempts += 1
            await self.db.put_otp(rec)
            raise AuthError("Incorrect code")
        await self.db.delete_otp(phone)
        user = await self.db.get_user_by_phone(phone) or await self.db.create_user(phone, ["buyer"], full_name)
        return await self._issue(user, family_id=str(uuid4()))

    # ---- tokens --------------------------------------------------------- #
    def access_token(self, user: User) -> str:
        now = _now()
        return jwt.encode({"sub": user.id, "roles": list(user.roles), "iss": ISSUER, "typ": "access",
                           "iat": int(now.timestamp()),
                           "exp": int((now + timedelta(seconds=self.settings.access_ttl_seconds)).timestamp())},
                          self.settings.jwt_secret, algorithm="HS256")

    def decode_access(self, token: str) -> dict:
        try:
            claims = jwt.decode(token, self.settings.jwt_secret, algorithms=["HS256"], issuer=ISSUER,
                                options={"require": ["exp", "sub", "iss"]})
        except jwt.ExpiredSignatureError as e:
            raise AuthError("Session expired") from e
        except jwt.InvalidTokenError as e:
            raise AuthError("Invalid token") from e
        if claims.get("typ") != "access":
            raise AuthError("Invalid token")
        return claims

    async def _issue(self, user: User, family_id: str) -> TokenPair:
        refresh = secrets.token_urlsafe(32)
        ttl = timedelta(days=self.settings.refresh_ttl_days)
        await self.db.put_refresh(RefreshRecord(token_hash=_sha256(refresh), user_id=user.id, family_id=family_id,
                                                expires_at=_now() + ttl))
        return TokenPair(self.access_token(user), self.settings.access_ttl_seconds, refresh,
                         int(ttl.total_seconds()), user)

    async def refresh(self, refresh_token: str) -> TokenPair:
        rec = await self.db.get_refresh(_sha256(refresh_token or ""))
        if not rec:
            raise AuthError("Not signed in")
        if rec.revoked_at is not None:
            # A rotated token came back: assume theft, kill every token in the chain.
            await self.db.revoke_refresh_family(rec.family_id)
            log.warning("refresh token reuse detected for user %s; family revoked", rec.user_id)
            raise AuthError("Session revoked. Please sign in again.")
        if rec.expires_at < _now():
            raise AuthError("Session expired. Please sign in again.")
        user = await self.db.get_user(rec.user_id)
        if not user:
            raise AuthError("Not signed in")
        await self.db.revoke_refresh(rec.token_hash)
        return await self._issue(user, rec.family_id)

    async def logout(self, refresh_token: str | None) -> None:
        if not refresh_token:
            return
        rec = await self.db.get_refresh(_sha256(refresh_token))
        if rec:
            await self.db.revoke_refresh_family(rec.family_id)
