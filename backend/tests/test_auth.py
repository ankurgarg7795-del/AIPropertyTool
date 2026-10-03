import time

import jwt
import pytest

from app.auth.service import AuthError, normalize_phone
from app.core.config import Settings
from tests.conftest import login


@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "+919876543210"), ("098765 43210", "+919876543210"), ("+91 98765-43210", "+919876543210"),
    ("919876543210", "+919876543210"), ("+14155550123", "+14155550123"),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["12345", "5876543210", "abc", ""])
def test_normalize_phone_rejects(raw):
    with pytest.raises(AuthError):
        normalize_phone(raw)


def _otp(client, phone="9876543210"):
    return client.post("/api/v1/auth/otp/request", json={"phone": phone}).json()["dev_code"]


def test_cookie_login_refresh_rotation_and_reuse_detection(client):
    code = _otp(client)
    r = client.post("/api/v1/auth/otp/verify", json={"phone": "9876543210", "code": code})
    assert r.status_code == 200 and r.json()["access_token"] is None  # web mode: tokens only in cookies
    assert client.get("/api/v1/auth/me").json()["phone_e164"] == "+919876543210"
    rt1 = client.cookies.get("apt_rt")

    r = client.post("/api/v1/auth/refresh")
    assert r.status_code == 200
    rt2 = client.cookies.get("apt_rt")
    assert rt2 and rt2 != rt1

    # Replaying the rotated token revokes the whole family, including rt2.
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": rt1})
    assert r.status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": rt2}).status_code == 401


def test_logout_revokes_refresh(client):
    code = _otp(client)
    r = client.post("/api/v1/auth/otp/verify", json={"phone": "9876543210", "code": code},
                    headers={"X-Auth-Mode": "token"}).json()
    assert client.post("/api/v1/auth/logout", json={"refresh_token": r["refresh_token"]}).status_code == 204
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": r["refresh_token"]}).status_code == 401


def test_wrong_code_attempt_limit(client):
    code = _otp(client)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert client.post("/api/v1/auth/otp/verify", json={"phone": "9876543210", "code": wrong}).status_code == 401
    # 6th try is refused even with the right code; a new code is required
    r = client.post("/api/v1/auth/otp/verify", json={"phone": "9876543210", "code": code})
    assert r.status_code == 429


def test_otp_is_single_use(client):
    code = _otp(client)
    assert client.post("/api/v1/auth/otp/verify", json={"phone": "9876543210", "code": code}).status_code == 200
    assert client.post("/api/v1/auth/otp/verify", json={"phone": "9876543210", "code": code}).status_code == 401


def test_otp_send_rate_limit(client):
    for _ in range(5):
        assert client.post("/api/v1/auth/otp/request", json={"phone": "9876543210"}).status_code == 200
    assert client.post("/api/v1/auth/otp/request", json={"phone": "9876543210"}).status_code == 429


def test_same_phone_same_user(client):
    a = client.get("/api/v1/auth/me", headers=login(client, "9876543210")).json()
    b = client.get("/api/v1/auth/me", headers=login(client, "+91 98765 43210")).json()
    assert a["id"] == b["id"]


def test_bad_and_expired_tokens_are_401_even_on_public_endpoints(client):
    assert client.post("/api/v1/ai/search", json={"query": "2 bhk pune"},
                       headers={"Authorization": "Bearer nonsense"}).status_code == 401
    secret = client.container.settings.jwt_secret
    me = client.get("/api/v1/auth/me", headers=login(client, "9876543210")).json()
    expired = jwt.encode({"sub": me["id"], "iss": "aipropertytool", "typ": "access", "exp": int(time.time()) - 5},
                         secret, algorithm="HS256")
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    forged = jwt.encode({"sub": me["id"], "iss": "aipropertytool", "typ": "access", "exp": int(time.time()) + 60},
                        "an-attacker-chosen-secret-that-is-long-enough", algorithm="HS256")
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_self_service_roles(client):
    h = login(client, "9876543210")
    assert "owner" in client.post("/api/v1/auth/roles", json={"role": "owner"}, headers=h).json()["roles"]
    assert client.post("/api/v1/auth/roles", json={"role": "admin"}, headers=h).status_code == 403
    assert client.post("/api/v1/auth/roles", json={"role": "agent"}, headers=h).status_code == 403


def test_otp_code_not_returned_unless_explicitly_enabled(client):
    client.container.settings.dev_otp_in_response = False
    r = client.post("/api/v1/auth/otp/request", json={"phone": "9876543210"}).json()
    assert r["dev_code"] is None and r["phone_e164"] == "+919876543210"


def test_prod_settings_refuse_insecure_defaults():
    with pytest.raises(RuntimeError):
        Settings(env="prod", jwt_secret="x" * 40, database_url="postgresql://x",
                 dev_otp_in_response=True).validate_for_env()
    with pytest.raises(RuntimeError):
        Settings(env="prod", database_url="postgresql://x").validate_for_env()
    with pytest.raises(RuntimeError):
        Settings(env="prod", jwt_secret="x" * 40).validate_for_env()
    Settings(env="prod", jwt_secret="x" * 40, database_url="postgresql://x").validate_for_env()
