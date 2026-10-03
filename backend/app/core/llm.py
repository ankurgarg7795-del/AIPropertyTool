"""Thin Claude wrapper: multimodal structured extraction + short text generation.

Every pipeline calls ``LLM.extract`` with a Pydantic model; the model's JSON
schema is sent as ``output_config.format`` so the response is guaranteed to be
schema-valid JSON. If no API key is configured, ``LLM.enabled`` is False and
callers fall back to deterministic heuristics, which keeps the MVP runnable
offline and makes the test-suite hermetic.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema -> structured-output schema.

    Inlines ``$ref``s, marks every property required (optional fields stay
    nullable via ``anyOf``) and forbids additional properties.
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
        out = {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"].keys())
            out["additionalProperties"] = False
        return out

    return walk(raw)


def image_block(data: bytes, media_type: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.b64encode(data).decode()}}


def pdf_block(data: bytes, title: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                          "data": base64.b64encode(data).decode()}}
    if title:
        block["title"] = title
    return block


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


class LLM:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client: anthropic.AsyncAnthropic | None = None
        if self.settings.llm_enabled:
            self._client = anthropic.AsyncAnthropic(api_key=self.settings.anthropic_api_key)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _request_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.settings.llm_model}
        if self.settings.llm_server_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    async def extract(
        self,
        *,
        system: str,
        content: list[dict[str, Any]],
        schema: type[T],
        effort: str | None = None,
        max_tokens: int = 16000,
    ) -> T:
        """Run one multimodal request and return a validated ``schema`` instance."""
        if not self._client:
            raise LLMError("LLM disabled: set ANTHROPIC_API_KEY")
        try:
            resp = await self._client.beta.messages.create(
                **self._request_kwargs(),
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": effort or self.settings.llm_effort,
                    "format": {"type": "json_schema", "schema": strict_json_schema(schema)},
                },
            )
        except anthropic.RateLimitError as e:
            raise LLMError(f"rate limited: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Claude API unreachable: {e}") from e

        if resp.stop_reason == "refusal":
            raise LLMRefusal(getattr(resp.stop_details, "explanation", None) or "request declined")
        if resp.stop_reason == "max_tokens":
            raise LLMError("structured output truncated (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMError("no text block in response")
        return schema.model_validate(json.loads(text))

    async def reply(self, *, system: str, messages: list[dict[str, Any]], max_tokens: int = 1024) -> str:
        """Short conversational reply (concierge). Low effort keeps latency chat-friendly."""
        if not self._client:
            raise LLMError("LLM disabled: set ANTHROPIC_API_KEY")
        try:
            resp = await self._client.beta.messages.create(
                **self._request_kwargs(),
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_config={"effort": "low"},
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise LLMError(str(e)) from e
        if resp.stop_reason == "refusal":
            raise LLMRefusal("request declined")
        return "".join(b.text for b in resp.content if b.type == "text").strip()


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm
