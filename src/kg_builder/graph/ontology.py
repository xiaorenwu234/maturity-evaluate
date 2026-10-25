"""Ontology for the Unified System Knowledge Graph.

This module is the single source of truth for the vocabulary used across the
whole of Module 1 (and, by extension, consumed by Module 2 "assessment & issue
discovery" and Module 3 "issue remediation").

It defines:

* :class:`NodeType`     -- the kinds of entities that can appear as graph nodes.
* :class:`RelationType` -- the kinds of directed edges between nodes.
* :class:`Source-Type`  -- where a piece of knowledge was collected from, which
  powers the evidence-chain (provenance) requirement.

The vocabulary intentionally mirrors the design spec:

Nodes   : Service, Module, API, Function, Config, Database, Deployment,
          Metric, Document, ExternalDependency
Relations: CALLS, DEPENDS_ON, DEPLOYED_AS, CONFIGURED_BY, READS, WRITES,
          EXPOSES, OBSERVED_BY, INHERITS, TEMPORALLY_COUPLED
"""
from __future__ import annotations

from enum import Enum


class NodeType(str, Enum):
    """Kinds of entities modelled as graph nodes."""

    SERVICE = "Service"
    MODULE = "Module"
    API = "API"
    FUNCTION = "Function"
    CONFIG = "Config"
    DATABASE = "Database"
    DEPLOYMENT = "Deployment"
    METRIC = "Metric"
    DOCUMENT = "Document"
    EXTERNAL_DEPENDENCY = "ExternalDependency"

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in {m.value for m in cls}


class RelationType(str, Enum):
    """Kinds of directed relations between nodes.

    Every relation is directed: ``src --RELATION--> dst``.
    """

    CALLS = "CALLS"              # service/function -> service/function
    DEPENDS_ON = "DEPENDS_ON"    # service/module -> external dependency / db / module
    DEPLOYED_AS = "DEPLOYED_AS"  # service -> deployment
    CONFIGURED_BY = "CONFIGURED_BY"  # service/function/config -> config
    READS = "READS"              # function/service -> database (read access)
    WRITES = "WRITES"            # function/service -> database (write access)
    EXPOSES = "EXPOSES"          # service/module -> api
    OBSERVED_BY = "OBSERVED_BY"  # service/deployment -> metric
    INHERITS = "INHERITS"        # service/class -> service/class (type hierarchy)
    TEMPORALLY_COUPLED = "TEMPORALLY_COUPLED"  # module -> module (co-changed in VCS)
    CLONE_OF = "CLONE_OF"        # function -> function (type-2 duplicate body)
    IMPLEMENTS = "IMPLEMENTS"    # service -> module/file (LLM/doc-derived mapping)

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in {m.value for m in cls}


class SourceType(str, Enum):
    """Origin category of a piece of knowledge (drives the evidence chain)."""

    SOURCE_CODE = "source_code"   # AST / static analysis
    DOCUMENT = "document"         # architecture / interface / config docs
    DEPLOYMENT = "deployment"     # Kubernetes / cloud control plane
    RUNTIME = "runtime"           # metrics / logs / traces
    HISTORY = "history"           # version-control change history (churn / co-change)
    LLM = "llm"                   # model-inferred semantics
    MANUAL = "manual"             # human-provided / curated

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in {m.value for m in cls}


# Human readable labels for node types, useful in reports / CLI output.
NODE_TYPE_LABELS: dict[str, str] = {
    NodeType.SERVICE.value: "服务",
    NodeType.MODULE.value: "模块",
    NodeType.API.value: "接口",
    NodeType.FUNCTION.value: "函数/方法",
    NodeType.CONFIG.value: "配置项",
    NodeType.DATABASE.value: "数据库",
    NodeType.DEPLOYMENT.value: "部署单元",
    NodeType.METRIC.value: "指标",
    NodeType.DOCUMENT.value: "文档",
    NodeType.EXTERNAL_DEPENDENCY.value: "外部依赖",
}
