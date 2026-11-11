"""Pydantic contracts for the document collector's LLM calls.

Each model mirrors one prompt's expected JSON shape. Handing it to
:meth:`LLMClient.complete_structured` is what guarantees the caller gets every
field back: keys the model left out fall through to the defaults declared here,
and the ``mode="before"`` validators normalise the two kinds of junk models
regularly emit for these prompts -- a bare string where an object was asked, and
the literal ``"null"`` where JSON ``null`` was.

Normalising instead of rejecting also matters in batch mode: one malformed entry
must not throw away the model's answer for the whole batch.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Values a model writes instead of JSON ``null`` for a nullable field.
_NULLISH = {"", "null", "none", "nil", "n/a", "na", "no", "-", "无", "未知"}

# Kept in sync with ``BaseConfigParser.category`` in ``document/utils``.
CONFIG_CATEGORIES = frozenset(
    {"k8s", "docker", "monitor", "ci", "app_config", "api_spec", "iac", "other"}
)

_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY"}
)


class _Contract(BaseModel):
    """Base for LLM contracts: unknown keys are tolerated, aliases round-trip."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    def to_payload(self) -> dict[str, Any]:
        """Dump back to the plain-dict shape the graph-emitting code reads."""
        return self.model_dump(by_alias=True)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _optional_text(value: Any) -> Optional[str]:
    """Read a nullable string, mapping ``"null"``/``"无"`` to :data:`None`."""
    text = _text(value)
    return None if text.lower() in _NULLISH else text


def _object_list(value: Any, key: str) -> list[Any]:
    """Coerce a list of objects, upgrading bare strings to ``{key: value}``.

    ``"apis": ["/api/orders"]`` is a common shorthand; dropping the item would
    silently lose the fact, and failing validation would drop the whole batch, so
    the string is placed under the field the item cannot do without.
    """
    if isinstance(value, BaseModel):
        return [value]
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    out: list[Any] = []
    for item in value:
        if isinstance(item, (dict, BaseModel)):
            out.append(item)
            continue
        text = _text(item)
        if text:
            out.append({key: text})
    return out


def _keyed_mapping(value: Any) -> dict[str, Any]:
    """Keep only path -> object entries of a batch response.

    The outer mapping is keyed by the file path we echoed to the model; a value
    that is not an object is reduced to ``{}`` so its file falls back to
    heuristics without affecting its siblings in the same batch.
    """
    if not isinstance(value, dict):
        return {}
    return {str(key): (item if isinstance(item, dict) else {}) for key, item in value.items()}


def pick_by_path(found: dict[str, Any], rel: str) -> Optional[Any]:
    """Look a batch result up by path, tolerating minor spelling drift.

    Models usually echo the path verbatim but sometimes add ``./`` or drop a
    leading directory; without a fallback the document quietly loses its
    model-derived facts.
    """
    if not found:
        return None
    wanted = rel.strip().lstrip("./")
    for key, value in found.items():
        candidate = str(key).strip().lstrip("./")
        if candidate == wanted or candidate.endswith("/" + wanted) or wanted.endswith("/" + candidate):
            return value
    return None


# ------------------------------------------------------------------ prose docs
class ServiceMention(_Contract):
    name: str = ""
    responsibility: str = ""

    @field_validator("name", "responsibility", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> str:
        return _text(value)


class ApiMention(_Contract):
    path: str = ""
    method: str = "ANY"
    description: str = ""

    @field_validator("path", "description", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> str:
        return _text(value)

    @field_validator("method", mode="before")
    @classmethod
    def _method(cls, value: Any) -> str:
        method = _text(value).upper()
        return method if method in _HTTP_METHODS else "ANY"


class ServiceRelationship(_Contract):
    # ``from`` is a Python keyword, so the field carries it as an alias; the
    # payload keys stay ``from``/``to`` for the graph code that reads them.
    source: str = Field(default="", alias="from")
    target: str = Field(default="", alias="to")
    type: str = "CALLS"
    description: str = ""

    @field_validator("source", "target", "description", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> str:
        return _text(value)

    @field_validator("type", mode="before")
    @classmethod
    def _kind(cls, value: Any) -> str:
        return _text(value).upper() or "CALLS"


class TextExtraction(_Contract):
    """One prose document: services, APIs, relationships and a summary."""

    services: list[ServiceMention] = []
    apis: list[ApiMention] = []
    relationships: list[ServiceRelationship] = []
    summary: str = ""

    @field_validator("services", mode="before")
    @classmethod
    def _services(cls, value: Any) -> list[Any]:
        return _object_list(value, "name")

    @field_validator("apis", mode="before")
    @classmethod
    def _apis(cls, value: Any) -> list[Any]:
        return _object_list(value, "path")

    @field_validator("relationships", mode="before")
    @classmethod
    def _relationships(cls, value: Any) -> list[Any]:
        return _object_list(value, "from")

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: Any) -> str:
        return _text(value)


class BatchTextExtraction(_Contract):
    """Batched prose extraction, keyed by the file path it was handed."""

    documents: dict[str, TextExtraction] = {}

    @field_validator("documents", mode="before")
    @classmethod
    def _documents(cls, value: Any) -> dict[str, Any]:
        return _keyed_mapping(value)


# ------------------------------------------------------------- config documents
class ConfigClassification(_Contract):
    category: str = "other"
    owner_service: Optional[str] = None
    summary: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> str:
        token = _text(value).lower().replace("-", "_").replace(" ", "_")
        return token if token in CONFIG_CATEGORIES else "other"

    @field_validator("owner_service", mode="before")
    @classmethod
    def _owner(cls, value: Any) -> Optional[str]:
        return _optional_text(value)

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: Any) -> str:
        return _text(value)


class BatchConfigClassification(_Contract):
    files: dict[str, ConfigClassification] = {}

    @field_validator("files", mode="before")
    @classmethod
    def _files(cls, value: Any) -> dict[str, Any]:
        return _keyed_mapping(value)


# ------------------------------------------------- cross-source service mapping
class ServiceMapping(_Contract):
    service_name: str = ""
    k8s_deployment: str = ""
    k8s_namespace: str = ""
    source_files: list[str] = []
    responsibility: str = ""

    @field_validator(
        "service_name", "k8s_deployment", "k8s_namespace", "responsibility", mode="before"
    )
    @classmethod
    def _strip(cls, value: Any) -> str:
        return _text(value)

    @field_validator("source_files", mode="before")
    @classmethod
    def _source_files(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if not isinstance(value, list):
            return []
        return [path for path in (_text(item) for item in value) if path]


class ServiceMappings(_Contract):
    services: list[ServiceMapping] = []

    @field_validator("services", mode="before")
    @classmethod
    def _services(cls, value: Any) -> list[Any]:
        return _object_list(value, "service_name")


__all__ = [
    "ApiMention",
    "BatchConfigClassification",
    "BatchTextExtraction",
    "CONFIG_CATEGORIES",
    "ConfigClassification",
    "ServiceMapping",
    "ServiceMappings",
    "ServiceMention",
    "ServiceRelationship",
    "TextExtraction",
    "pick_by_path",
]
