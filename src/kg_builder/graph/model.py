"""Core data model for the Unified System Knowledge Graph.

Design highlights (aligned with the module spec):

* A node/edge is not just a bare entity -- it carries **provenance**. The spec
  explicitly requires the graph to remember *where* a fact came from, e.g.::

      Payment --replica--> 1     source: Kubernetes
      Order    --calls--> Payment source: source code

  so that downstream Module 2 can produce a full evidence chain when it flags
  an issue. A single logical fact may be corroborated by several sources, so
  ``provenance`` is a *list*.

* Identifiers are deterministic and content-derived so repeated builds produce
  the same ids and facts from different collectors *merge* instead of duplicating.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .ontology import NodeType, RelationType, SourceType


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_node_id(node_type: str, canonical_name: str, namespace: str = "") -> str:
    """Build a deterministic node id from a type + canonical name.

    The same entity discovered by two collectors (e.g. code says ``Order``
    service, deployment says ``order`` Deployment mapping to the same service)
    resolves to the same id so their provenance is merged.
    """
    base = f"{node_type}:{namespace}:{canonical_name}".lower().strip()
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    # Keep the human-readable prefix for debugging, plus a stable hash suffix.
    slug = canonical_name.replace(" ", "_").replace("/", "_")
    return f"{node_type}/{slug}#{digest}"


@dataclass
class Provenance:
    """Evidence describing how a single fact was obtained.

    Attributes:
        source_type: One of :class:`SourceType` (code / document / deployment /
            runtime / llm / manual).
        source_id: A pointer to the concrete origin, e.g. a file path + line,
            a Kubernetes ``cluster/namespace/kind/name``, a document section.
        evidence: The raw supporting snippet / quote / record that justifies the
            fact. Kept verbatim when feasible so Module 2 can cite it directly.
        confidence: ``0.0..1.0``. Static-analysis facts are usually ``1.0``;
            LLM-inferred semantics are lower.
        detail: Optional structured extras (language, parser, api-version...).
        collected_at: UTC ISO timestamp of collection.
    """

    source_type: str
    source_id: str
    evidence: str = ""
    confidence: float = 1.0
    detail: dict[str, Any] = field(default_factory=dict)
    collected_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        # Normalize enum members to their string values for clean serialization.
        if isinstance(self.source_type, SourceType):
            self.source_type = self.source_type.value
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 4),
            "detail": self.detail,
            "collected_at": self.collected_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Provenance":
        return cls(
            source_type=data["source_type"],
            source_id=data.get("source_id", ""),
            evidence=data.get("evidence", ""),
            confidence=data.get("confidence", 1.0),
            detail=data.get("detail", {}) or {},
            collected_at=data.get("collected_at", _now_iso()),
        )


@dataclass
class KGNode:
    """A node in the knowledge graph."""

    id: str
    type: str
    name: str                       # human-readable label, e.g. "OrderService"
    canonical_name: str = ""        # normalized key used for merging
    namespace: str = ""             # repo / cluster / domain grouping
    attributes: dict[str, Any] = field(default_factory=dict)
    provenance: list[Provenance] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.type, NodeType):
            self.type = self.type.value
        if not self.canonical_name:
            self.canonical_name = self.name

    @property
    def sources(self) -> set[str]:
        """Distinct source-type set that attest to this node."""
        return {p.source_type for p in self.provenance}

    def add_provenance(self, prov: Provenance) -> None:
        """Merge provenance, avoiding exact duplicates."""
        for existing in self.provenance:
            if (
                existing.source_type == prov.source_type
                and existing.source_id == prov.source_id
                and existing.evidence == prov.evidence
            ):
                # Keep the higher confidence if re-attested.
                existing.confidence = max(existing.confidence, prov.confidence)
                return
        self.provenance.append(prov)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "canonical_name": self.canonical_name,
            "namespace": self.namespace,
            "attributes": self.attributes,
            "provenance": [p.to_dict() for p in self.provenance],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KGNode":
        return cls(
            id=data["id"],
            type=data["type"],
            name=data["name"],
            canonical_name=data.get("canonical_name", data["name"]),
            namespace=data.get("namespace", ""),
            attributes=data.get("attributes", {}) or {},
            provenance=[Provenance.from_dict(p) for p in data.get("provenance", [])],
        )


@dataclass
class KGEdge:
    """A directed relation between two nodes, carrying its own provenance."""

    source: str          # node id
    target: str          # node id
    relation: str        # RelationType value
    attributes: dict[str, Any] = field(default_factory=dict)
    provenance: list[Provenance] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.relation, RelationType):
            self.relation = self.relation.value

    @property
    def key(self) -> tuple[str, str, str]:
        """Merge key: two edges with the same (src, dst, relation) are one fact."""
        return (self.source, self.target, self.relation)

    def add_provenance(self, prov: Provenance) -> None:
        for existing in self.provenance:
            if (
                existing.source_type == prov.source_type
                and existing.source_id == prov.source_id
                and existing.evidence == prov.evidence
            ):
                existing.confidence = max(existing.confidence, prov.confidence)
                return
        self.provenance.append(prov)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "attributes": self.attributes,
            "provenance": [p.to_dict() for p in self.provenance],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KGEdge":
        return cls(
            source=data["source"],
            target=data["target"],
            relation=data["relation"],
            attributes=data.get("attributes", {}) or {},
            provenance=[Provenance.from_dict(p) for p in data.get("provenance", [])],
        )
