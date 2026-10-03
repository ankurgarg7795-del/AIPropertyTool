"""Fixtures. Every API/flow test runs on the in-memory backend and, when
``TEST_DATABASE_URL`` is set, again on PostgreSQL (migrated once, truncated per test)."""

import asyncio
import json
import os
from types import SimpleNamespace

import psycopg
import pytest

os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("WHATSAPP_APP_SECRET", None)

TEST_DB = os.environ.get("TEST_DATABASE_URL")
BACKENDS = ["memory"] + (["postgres"] if TEST_DB else [])


class FakeMessages:
    """Records request kwargs and returns canned JSON, standing in for client.beta.messages."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.responses.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return SimpleNamespace(stop_reason="end_turn", stop_details=None,
                               content=[SimpleNamespace(type="text", text=text)])


@pytest.fixture
def fake_llm():
    from app.core.config import Settings
    from app.core.llm import LLM

    def make(*responses):
        llm = LLM(Settings(anthropic_api_key="test-key"))
        fake = FakeMessages(responses)
        llm._client = SimpleNamespace(beta=SimpleNamespace(messages=fake))
        return llm, fake

    return make


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def _pg_migrated():
    from app.db.migrate import migrate

    asyncio.run(migrate(TEST_DB))
    return TEST_DB


def _truncate(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'")]
        conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path):
    from app.core.config import Settings

    db_url = None
    if request.param == "postgres":
        db_url = request.getfixturevalue("_pg_migrated")
        _truncate(db_url)
    return Settings(env="test", database_url=db_url, auto_migrate=False, media_dir=str(tmp_path / "media"),
                    seed_demo_data=True, dev_otp_in_response=True)


def _container(settings):
    from app.container import Container
    from app.core.config import Settings
    from app.core.llm import LLM

    return Container(llm=LLM(Settings()), settings=settings)


@pytest.fixture
async def container(backend):
    c = _container(backend)
    await c.start()
    yield c
    await c.stop()


@pytest.fixture
def client(backend):
    import app.container as cont
    from fastapi.testclient import TestClient

    from app.main import app

    cont._container = _container(backend)
    with TestClient(app) as tc:
        tc.container = cont._container
        yield tc
    cont._container = None


def login(client, phone: str, roles: tuple[str, ...] = ()) -> dict[str, str]:
    """Sign in via OTP (token mode) and return Authorization headers."""
    code = client.post("/api/v1/auth/otp/request", json={"phone": phone}).json()["dev_code"]
    r = client.post("/api/v1/auth/otp/verify", json={"phone": phone, "code": code},
                    headers={"X-Auth-Mode": "token"})
    assert r.status_code == 200, r.text
    body = r.json()
    for role in roles:
        client.portal.call(client.container.db.add_role, body["user"]["id"], role)
    return {"Authorization": f"Bearer {body['access_token']}"}
