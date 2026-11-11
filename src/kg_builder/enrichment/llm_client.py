"""LLM client for semantic enrichment (LangChain + OpenAI-compatible endpoint).

There is no offline / null fallback here: enrichment is performed *only* by a
model. If the model is not configured, :func:`create_llm_client` raises, and the
caller must either configure it (see :mod:`kg_builder.config`) or skip the
enrichment step entirely.

Two call styles are offered:

* :meth:`LLMClient.complete_json` -- the model picks the shape, the caller
  defends against whatever comes back.
* :meth:`LLMClient.complete_structured` -- the caller passes a Pydantic model and
  gets a validated instance back, so every declared field exists. Endpoints that
  support native structured output (tool calling / ``json_schema``) enforce the
  schema at the API level; the rest are asked for the schema as JSON and
  re-asked once when the answer does not validate.

Any OpenAI-compatible chat endpoint is supported by pointing ``base_url`` at it
(OpenAI, Qwen/DashScope, DeepSeek, a self-hosted vLLM/TGI gateway, ...).
"""
from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Optional, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import LLMSettings

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


def _schema_hint(schema: type[BaseModel]) -> str:
    """Render *schema* as a JSON Schema line to append to a system prompt.

    Only used by the JSON fallback: endpoints with native structured output get
    the schema through the API instead of the prompt text.
    """
    compact = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return f"输出必须是符合以下 JSON Schema 的 JSON 对象：{compact}"


def _reask_prompt(user: str, errors: ValidationError) -> str:
    """Rebuild the user prompt so the model can see what it got wrong."""
    problems = "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
        for err in errors.errors()[:8]
    )
    return (
        f"{user}\n\n上一次输出不符合要求（{problems}）。"
        "请重新输出完整 JSON，覆盖所有字段，不要只输出修正的部分。"
    )


class LLMClient(ABC):
    """Minimal chat interface returning structured JSON."""

    #: Attempts made by the JSON fallback of :meth:`complete_structured`.
    structured_attempts = 2

    @abstractmethod
    def complete_json(self, system: str, user: str) -> Optional[dict]:
        """Return a parsed JSON object from the model.

        Raises :class:`LLMError` on transport / parse failure so misconfiguration
        is loud rather than silently swallowed into heuristics.
        """

    def complete_structured(self, system: str, user: str, schema: type[M]) -> M:
        """Return an instance of *schema*, with every declared field present.

        Fields the model omitted fall back to the defaults declared on
        ``schema``; output that cannot be reconciled with it is re-asked once
        (with the validation errors inlined) before :class:`LLMError` is raised.
        """
        return _validate_json_calls(self, system, user, schema)

    @property
    def available(self) -> bool:
        return True


def call_structured(client: Any, system: str, user: str, schema: type[M]) -> M:
    """Ask *client* for output conforming to *schema*.

    Use this instead of ``client.complete_structured`` when the client may be any
    JSON-speaking object: it takes the client's own structured implementation
    when present (native schema enforcement) and otherwise validates the
    client's ``complete_json`` answer. So a caller only has to name the data
    class, not care what the endpoint can do.
    """
    own = getattr(client, "complete_structured", None)
    if callable(own):
        return own(system, user, schema)
    return _validate_json_calls(client, system, user, schema)


def _validate_json_calls(client: Any, system: str, user: str, schema: type[M]) -> M:
    """Schema-hinted JSON calls plus validation, re-asking on a miss."""
    system_prompt = f"{system}\n{_schema_hint(schema)}"
    prompt = user
    last_errors: Optional[ValidationError] = None
    attempts = getattr(client, "structured_attempts", LLMClient.structured_attempts)
    for attempt in range(attempts):
        data = client.complete_json(system_prompt, prompt)
        if data is None:
            raise LLMError(f"{schema.__name__}: model returned no JSON object")
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            last_errors = exc
            if attempt + 1 < attempts:
                prompt = _reask_prompt(user, exc)
    raise LLMError(f"model output does not match {schema.__name__}: {last_errors}")


class LLMError(RuntimeError):
    """Raised when the model cannot be reached or returns unparseable output."""


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)


def _parse_json_loose(text: str) -> dict:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    raise LLMError(f"model did not return a JSON object; got: {text[:400]!r}")


class LangChainLLMClient(LLMClient):
    """LangChain ``ChatOpenAI`` pointed at an OpenAI-compatible endpoint."""

    def __init__(self, settings: LLMSettings) -> None:
        settings.require_configured()
        try:
            from langchain_openai import ChatOpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "langchain-openai is required for semantic enrichment (a core dependency). "
                "Reinstall with `conda run -n CMMeter pip install -e .` or skip enrichment "
                "with --skip-enrichment."
            ) from exc

        self._settings = settings
        model_kwargs: dict = {}
        if settings.json_mode:
            model_kwargs["response_format"] = {"type": "json_object"}

        self._chat = ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
            timeout=settings.request_timeout,
            model_kwargs=model_kwargs,
        )
        # One structured-output chain per schema class (building one per call
        # rebuilds the tool spec for every document batch).
        self._structured_chains: dict[str, Any] = {}
        self._native_structured = True

    @staticmethod
    def _messages(system: str, user: str) -> list:
        from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore

        return [SystemMessage(content=system), HumanMessage(content=user)]

    def _structured_chain(self, schema: type[BaseModel]) -> Any:
        key = schema.__name__
        if key not in self._structured_chains:
            self._structured_chains[key] = self._chat.with_structured_output(schema)
        return self._structured_chains[key]

    def complete_structured(self, system: str, user: str, schema: type[M]) -> M:
        """Ask the endpoint itself to enforce *schema*, then validate anyway.

        A schema sent through tool calling / ``json_schema`` constrains the
        model's output, which is stronger than asking for it in prose. Endpoints
        that cannot do it are detected here and permanently switched to the
        JSON fallback, so the capability is only probed once per run.
        """
        native = self._invoke_native(system, user, schema)
        if native is not None:
            return native
        return super().complete_structured(system, user, schema)

    def _invoke_native(self, system: str, user: str, schema: type[M]) -> Optional[M]:
        """Return a validated instance via native output, or ``None`` to fall back."""
        if not self._native_structured:
            return None
        try:
            result = self._structured_chain(schema).invoke(self._messages(system, user))
        except ValidationError as exc:
            logger.warning(f"structured output for {schema.__name__} did not validate: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 - endpoint has no structured output
            logger.warning(
                f"endpoint cannot do native structured output ({exc}); "
                "falling back to JSON + schema validation"
            )
            self._native_structured = False
            return None
        if isinstance(result, schema):
            return result
        if isinstance(result, dict):
            try:
                return schema.model_validate(result)
            except ValidationError as exc:
                logger.warning(f"structured output for {schema.__name__} did not validate: {exc}")
                return None
        logger.warning(
            f"native structured output returned {type(result).__name__}, not {schema.__name__}"
        )
        return None

    def complete_json(self, system: str, user: str) -> dict:
        try:
            resp = self._chat.invoke(self._messages(system, user))
        except Exception as exc:  # noqa: BLE001 - surface as LLMError (no fallback)
            raise LLMError(f"LLM request failed: {exc}") from exc
        content = getattr(resp, "content", "") or ""
        if isinstance(content, list):  # some backends return content blocks
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        return _parse_json_loose(content)


def create_llm_client(settings: LLMSettings) -> LLMClient:
    """Build the LLM client. Raises :class:`LLMError` if it cannot be configured.

    No silent degradation to heuristics -- misconfiguration fails fast.
    """
    return LangChainLLMClient(settings)
