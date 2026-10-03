"""FastAPI entrypoint: ``uvicorn app.main:app --reload``."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.auth.routes import router as auth_router
from app.container import get_container

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    c = get_container()
    await c.start()
    logging.getLogger("app").info(
        "env=%s storage=%s LLM %s (model=%s)", c.settings.env, "postgres" if c.settings.database_url else "memory",
        "enabled" if c.llm.enabled else "DISABLED - heuristic mode", c.settings.llm_model)
    yield
    await c.stop()


app = FastAPI(title="AIPropertyTool API", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_container().settings.cors_origins, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(auth_router)
app.include_router(router)


@app.get("/healthz")
async def healthz():
    c = get_container()
    return {"ok": True, "llm_enabled": c.llm.enabled, "storage": "postgres" if c.settings.database_url else "memory",
            "listings": await c.db.count_listings()}
