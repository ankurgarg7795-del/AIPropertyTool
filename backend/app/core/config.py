"""Runtime configuration, loaded from environment variables (prefix ``APT_``)."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel


class Settings(BaseModel):
    # LLM
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    # Route refused requests to a fallback model server-side (Claude API only).
    llm_server_fallbacks: bool = True

    # Embeddings: "hashing" (offline, deterministic) or "sentence-transformers"
    embedder: str = "hashing"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 384

    # Media
    max_upload_mb: int = 200
    video_keyframes: int = 6

    # Seed demo inventory on startup
    seed_demo_data: bool = True

    cors_origins: list[str] = ["http://localhost:3000"]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    env = os.environ
    return Settings(
        anthropic_api_key=env.get("ANTHROPIC_API_KEY") or None,
        llm_model=env.get("APT_LLM_MODEL", "claude-opus-5-5"),
        llm_effort=env.get("APT_LLM_EFFORT", "medium"),
        llm_server_fallbacks=env.get("APT_LLM_SERVER_FALLBACKS", "true").lower() == "true",
        embedder=env.get("APT_EMBEDDER", "hashing"),
        embedding_model=env.get("APT_EMBEDDING_MODEL", "BAAI/bge-m3"),
        embedding_dim=int(env.get("APT_EMBEDDING_DIM", "384")),
        max_upload_mb=int(env.get("APT_MAX_UPLOAD_MB", "200")),
        video_keyframes=int(env.get("APT_VIDEO_KEYFRAMES", "6")),
        seed_demo_data=env.get("APT_SEED_DEMO_DATA", "true").lower() == "true",
        cors_origins=[o.strip() for o in env.get("APT_CORS_ORIGINS", "http://localhost:3000").split(",")],
    )
