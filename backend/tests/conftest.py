import json
import os
from types import SimpleNamespace

import pytest

os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["APT_SEED_DEMO_DATA"] = "true"


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


@pytest.fixture
def container():
    from app.container import Container
    from app.core.config import Settings
    from app.core.llm import LLM

    c = Container(llm=LLM(Settings()))
    c.seed()
    return c


@pytest.fixture
def client():
    import app.container as cont
    from fastapi.testclient import TestClient

    cont._container = None
    from app.main import app

    with TestClient(app) as tc:
        yield tc
    cont._container = None
