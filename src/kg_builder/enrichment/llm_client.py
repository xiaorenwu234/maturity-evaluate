"""LLM client for semantic enrichment (LangChain + OpenAI-compatible endpoint).

There is no offline / null fallback here: enrichment is performed *only* by a
model. If the model is not configured, :func:`create_llm_client` raises, and the
caller must either configure it (see :mod:`kg_builder.config`) or skip the
enrichment step entirely.

Any OpenAI-compatible chat endpoint is supported by pointing ``base_url`` at it
(OpenAI, Qwen/DashScope, DeepSeek, a self-hosted vLLM/TGI gateway, ...).
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Optional

from ..config import LLMSettings


class LLMClient(ABC):
    """Minimal chat interface returning structured JSON."""

    @abstractmethod
    def complete_json(self, system: str, user: str) -> Optional[dict]:
        """Return a parsed JSON object from the model.

        Raises :class:`LLMError` on transport / parse failure so misconfiguration
        is loud rather than silently swallowed into heuristics.
        """

    @property
    def available(self) -> bool:
        return True


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

    def complete_json(self, system: str, user: str) -> dict:
        from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore

        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        try:
            resp = self._chat.invoke(messages)
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
