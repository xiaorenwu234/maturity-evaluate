"""The Unified System Knowledge Graph container.

This is the *only* artifact Module 1 produces and the *only* data entry point
for Module 2 (assessment) and Module 3 (remediation). It wraps a ``networkx``
multi-fact graph while preserving first-class :class:`KGNode` / :class:`KGEdge`
objects (and their provenance lists) as node/edge attributes.

Merging semantics: adding a node/edge that already exists merges provenance
instead of overwriting, so facts from code + documents + deployment + runtime
accumulate onto the same logical entity.
"""
from __future__ import annotations

from typing import Iterable, Optional

import networkx as nx

from .model import KGEdge, KGNode, make_node_id
from .ontology import NodeType, RelationType


class KnowledgeGraph:
    """In-memory unified knowledge graph."""

    def __init__(self) -> None:
        self._nodes: dict[str, KGNode] = {}
        self._edges: dict[tuple[str, str, str], KGEdge] = {}
        # Document-level analysis metadata (e.g. clone-group statistics).
        self.meta: dict = {}

    # ------------------------------------------------------------------ nodes
    def add_node(
        self,
        node: KGNode,
    ) -> KGNode:
        """Add or merge a node. Returns the canonical stored node."""
        existing = self._nodes.get(node.id)
        if existing is None:
            self._nodes[node.id] = node
            return node
        # Merge: union attributes (existing wins on conflict), merge provenance.
        for k, v in node.attributes.items():
            existing.attributes.setdefault(k, v)
        for prov in node.provenance:
            existing.add_provenance(prov)
        return existing

    def upsert_node(
        self,
        node_type: NodeType | str,
        name: str,
        canonical_name: Optional[str] = None,
        namespace: str = "",
        attributes: Optional[dict] = None,
        provenance: Optional[Iterable] = None,
    ) -> KGNode:
        """Convenience factory + add for a node."""
        type_val = node_type.value if isinstance(node_type, NodeType) else str(node_type)
        canon = canonical_name or name
        node = KGNode(
            id=make_node_id(type_val, canon, namespace),
            type=type_val,
            name=name,
            canonical_name=canon,
            namespace=namespace,
            attributes=attributes or {},
            provenance=list(provenance or []),
        )
        return self.add_node(node)

    def get_node(self, node_id: str) -> Optional[KGNode]:
        return self._nodes.get(node_id)

    def find_nodes(
        self,
        type: Optional[NodeType | str] = None,
        name: Optional[str] = None,
    ) -> list[KGNode]:
        type_val = type.value if isinstance(type, NodeType) else type
        out: list[KGNode] = []
        for node in self._nodes.values():
            if type_val is not None and node.type != type_val:
                continue
            if name is not None and node.canonical_name.lower() != name.lower():
                continue
            out.append(node)
        return out

    @property
    def nodes(self) -> list[KGNode]:
        return list(self._nodes.values())

    # ------------------------------------------------------------------ edges
    def add_edge(self, edge: KGEdge) -> Optional[KGEdge]:
        """Add or merge an edge. Both endpoints must already exist."""
        if edge.source not in self._nodes or edge.target not in self._nodes:
            raise KeyError(
                f"Cannot add edge {edge.source}->{edge.target} ({edge.relation}): "
                "endpoint not found. Add nodes before edges."
            )
        existing = self._edges.get(edge.key)
        if existing is None:
            self._edges[edge.key] = edge
            return edge
        for k, v in edge.attributes.items():
            existing.attributes.setdefault(k, v)
        for prov in edge.provenance:
            existing.add_provenance(prov)
        return existing

    def upsert_edge(
        self,
        source_id: str,
        target_id: str,
        relation: RelationType | str,
        attributes: Optional[dict] = None,
        provenance: Optional[Iterable] = None,
    ) -> Optional[KGEdge]:
        rel = relation.value if isinstance(relation, RelationType) else str(relation)
        edge = KGEdge(
            source=source_id,
            target=target_id,
            relation=rel,
            attributes=attributes or {},
            provenance=list(provenance or []),
        )
        return self.add_edge(edge)

    @property
    def edges(self) -> list[KGEdge]:
        return list(self._edges.values())

    def out_edges(self, node_id: str, relation: Optional[RelationType | str] = None) -> list[KGEdge]:
        rel = relation.value if isinstance(relation, RelationType) else relation
        return [
            e
            for e in self._edges.values()
            if e.source == node_id and (rel is None or e.relation == rel)
        ]

    def in_edges(self, node_id: str, relation: Optional[RelationType | str] = None) -> list[KGEdge]:
        rel = relation.value if isinstance(relation, RelationType) else relation
        return [
            e
            for e in self._edges.values()
            if e.target == node_id and (rel is None or e.relation == rel)
        ]

    # -------------------------------------------------------------- nx export
    def to_networkx(self) -> nx.MultiDiGraph:
        """Export to a ``networkx.MultiDiGraph`` (for Module 2 graph algorithms)."""
        g = nx.MultiDiGraph()
        for node in self._nodes.values():
            g.add_node(node.id, **node.to_dict())
        for edge in self._edges.values():
            g.add_edge(edge.source, edge.target, key=edge.relation, **edge.to_dict())
        return g

    # ------------------------------------------------------------- stats/meta
    def stats(self) -> dict:
        by_type: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for node in self._nodes.values():
            by_type[node.type] = by_type.get(node.type, 0) + 1
            for p in node.provenance:
                by_source[p.source_type] = by_source.get(p.source_type, 0) + 1
        rel_count: dict[str, int] = {}
        for edge in self._edges.values():
            rel_count[edge.relation] = rel_count.get(edge.relation, 0) + 1
        return {
            "node_count": len(self._nodes),
            "edge_count": len(self._edges),
            "nodes_by_type": by_type,
            "relations_by_type": rel_count,
            "provenance_by_source": by_source,
        }
