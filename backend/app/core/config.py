"""Runtime configuration, loaded from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel

DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me"


class Settings(BaseModel):
    env: Literal["dev", "test", "prod"] = "dev"

    # Storage: empty -> in-memory backend
    database_url: str | None = None
    auto_migrate: bool = True
    media_dir: str = "./var/media"

    # Auth
    jwt_secret: str = DEV_JWT_SECRET
    access_ttl_seconds: int = 15 * 60
    refresh_ttl_days: int = 30
    otp_ttl_seconds: int = 5 * 60
    otp_max_sends_per_hour: int = 5
    otp_max_attempts: int = 5
    # Return the OTP in the API response instead of only texting it. Local dev/tests only;
    # refused in prod so a misconfigured deployment can't hand out login codes.
    dev_otp_in_response: bool = False

    # LLM
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    # Route refused requests to a fallback model server-side (Claude API only).
    llm_server_fallbacks: bool = True

    # Embeddings: "hashing" (offline, deterministic) or "sentence-transformers".
    # Dimension must match the vector(1024) columns in sql/001_schema.sql.
    embedder: str = "hashing"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # Media
    max_upload_mb: int = 200
    video_keyframes: int = 6

    # Seed demo inventory on startup (skipped when listings already exist)
    seed_demo_data: bool = True

    cors_origins: list[str] = ["http://localhost:3000"]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def cookie_secure(self) -> bool:
        return self.env == "prod"

    def validate_for_env(self) -> None:
        if self.env == "prod":
            if self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret) < 32:
                raise RuntimeError("APT_JWT_SECRET must be set to a random value of >= 32 chars in prod")
            if not self.database_url:
                raise RuntimeError("DATABASE_URL is required in prod")
            if self.dev_otp_in_response:
                raise RuntimeError("APT_DEV_OTP_IN_RESPONSE must not be enabled in prod")


def _bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


@lru_cache
def get_settings() -> Settings:
    env = os.environ
    return Settings(
        env=env.get("APT_ENV", "dev"),
        database_url=env.get("DATABASE_URL") or None,
        auto_migrate=_bool("APT_AUTO_MIGRATE", "true"),
        media_dir=env.get("APT_MEDIA_DIR", "./var/media"),
        jwt_secret=env.get("APT_JWT_SECRET", DEV_JWT_SECRET),
        dev_otp_in_response=_bool("APT_DEV_OTP_IN_RESPONSE", "false"),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY") or None,
        llm_model=env.get("APT_LLM_MODEL", "claude-opus-5-5"),
        llm_effort=env.get("APT_LLM_EFFORT", "medium"),
        llm_server_fallbacks=_bool("APT_LLM_SERVER_FALLBACKS", "true"),
        embedder=env.get("APT_EMBEDDER", "hashing"),
        embedding_model=env.get("APT_EMBEDDING_MODEL", "BAAI/bge-m3"),
        embedding_dim=int(env.get("APT_EMBEDDING_DIM", "1024")),
        max_upload_mb=int(env.get("APT_MAX_UPLOAD_MB", "200")),
        video_keyframes=int(env.get("APT_VIDEO_KEYFRAMES", "6")),
        seed_demo_data=_bool("APT_SEED_DEMO_DATA", "true"),
        cors_origins=[o.strip() for o in env.get("APT_CORS_ORIGINS", "http://localhost:3000").split(",")],
    )
