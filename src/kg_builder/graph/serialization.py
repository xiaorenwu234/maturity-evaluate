"""Serialization for the Unified System Knowledge Graph.

Primary format is a portable JSON document (the persisted artifact Module 2 /
Module 3 consume). We also provide:

* ``to_mermaid``  -- quick visualisation for docs / demos.
* ``to_cypher``   -- Neo4j import statements, for when the graph should live in
  a real graph database later (the in-memory ``KnowledgeGraph`` stays the source
  of truth during Module 1).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .knowledge_graph import KnowledgeGraph
from .model import KGEdge, KGNode

SCHEMA_VERSION = "1.0"


def graph_to_dict(graph: KnowledgeGraph) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "stats": graph.stats(),
        "meta": graph.meta,
        "nodes": [n.to_dict() for n in graph.nodes],
        "edges": [e.to_dict() for e in graph.edges],
    }


def dict_to_graph(data: dict[str, Any]) -> KnowledgeGraph:
    graph = KnowledgeGraph()
    graph.meta = data.get("meta", {}) or {}
    # Nodes first (edges require endpoints to exist).
    for nd in data.get("nodes", []):
        graph.add_node(KGNode.from_dict(nd))
    for ed in data.get("edges", []):
        try:
            graph.add_edge(KGEdge.from_dict(ed))
        except KeyError:
            # Skip edges whose endpoints are missing (defensive load).
            continue
    return graph


def save_graph(graph: KnowledgeGraph, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(graph_to_dict(graph), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_graph(path: str | Path) -> KnowledgeGraph:
    path = Path(path)
    return dict_to_graph(json.loads(path.read_text(encoding="utf-8")))


def to_mermaid(graph: KnowledgeGraph, max_edges: int = 200) -> str:
    """Render a Mermaid ``graph TD`` document for quick visual inspection."""
    lines = ["graph TD"]
    safe_index = {node.id: f"n{i}" for i, node in enumerate(graph.nodes)}
    for node in graph.nodes:
        alias = safe_index[node.id]
        label = f"{node.name}\\n[{node.type}]"
        lines.append(f'    {alias}["{label}"]')
    for i, edge in enumerate(graph.edges):
        if i >= max_edges:
            lines.append(f"    %% ... truncated at {max_edges} edges")
            break
        s = safe_index.get(edge.source)
        t = safe_index.get(edge.target)
        if s and t:
            lines.append(f"    {s} -->|{edge.relation}| {t}")
    return "\n".join(lines)


def to_cypher(graph: KnowledgeGraph) -> str:
    """Emit valid, importable Neo4j Cypher (``MERGE`` upserts).

    Node/edge *properties are scalar* (Neo4j cannot store nested objects), so
    ``attributes`` and the full ``provenance`` list are stored as JSON strings and
    the distinct source set as a string list -- the evidence chain is preserved and
    queryable (e.g. ``n.provenance CONTAINS \"llm\"``). Loads with ``cypher-shell``
    or paste into Neo4j Browser.
    """

    def _s(value: str) -> str:
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return '"' + escaped + '"'

    def _json(value: Any) -> str:
        return _s(json.dumps(value, ensure_ascii=False))

    def _list(items: list[str]) -> str:
        return "[" + ", ".join(_s(str(i)) for i in items) + "]"

    type_of = {node.id: node.type for node in graph.nodes}
    labels = sorted({node.type for node in graph.nodes})

    lines: list[str] = ["// unified system knowledge graph -> Neo4j", ""]
    # Uniqueness constraint + id index per label (idempotent, speeds up MERGE).
    for lab in labels:
        lines.append(f"CREATE INDEX kg_idx_{lab} IF NOT EXISTS FOR (n:`{lab}`) ON (n.id);")
    lines.append("")

    for node in graph.nodes:
        prov = [p.to_dict() for p in node.provenance]
        lines.append(
            f"MERGE (n:`{node.type}` {{id: {_s(node.id)}}}) SET "
            f"n.name = {_s(node.name)}, "
            f"n.canonical_name = {_s(node.canonical_name)}, "
            f"n.namespace = {_s(node.namespace)}, "
            f"n.type = {_s(node.type)}, "
            f"n.attributes = {_json(node.attributes)}, "
            f"n.sources = {_list(sorted(node.sources))}, "
            f"n.provenance = {_json(prov)};"
        )
    lines.append("")

    for edge in graph.edges:
        st, tt = type_of.get(edge.source), type_of.get(edge.target)
        if not st or not tt:
            continue  # endpoint not in graph; skip rather than emit a broken match
        prov = [p.to_dict() for p in edge.provenance]
        src = sorted({p.source_type for p in edge.provenance})
        lines.append(
            f"MATCH (a:`{st}` {{id: {_s(edge.source)}}}), (b:`{tt}` {{id: {_s(edge.target)}}}) "
            f"MERGE (a)-[r:`{edge.relation}`]->(b) SET "
            f"r.relation = {_s(edge.relation)}, "
            f"r.attributes = {_json(edge.attributes)}, "
            f"r.sources = {_list(src)}, "
            f"r.provenance = {_json(prov)};"
        )
    return "\n".join(lines) + "\n"
