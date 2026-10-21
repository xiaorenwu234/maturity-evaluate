"""Import a built knowledge graph into a live Neo4j database.

Design notes
------------
* **Parameterized UNWIND batches**, not a dumped ``.cypher`` text file. Re-running
  the generated script text would require fragile statement-splitting (evidence
  strings can contain ``;`` / quotes). The driver sends typed values instead, so
  escaping is the driver's problem and there is none.
* Neo4j properties must be scalars or homogeneous arrays, so the nested
  ``attributes`` / ``provenance`` (the evidence chain) are stored as JSON *strings*
  and the source set as a string array. Query them with ``... CONTAINS ...`` or
  ``apoc.convert.fromJsonMap`` later.
* Labels and relationship types are validated as safe identifiers (they come from
  the controlled ontology, but this guards against injection via odd names).
* Idempotent: ``MERGE`` on the deterministic ``id`` -- re-importing upserts.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Iterator

from .knowledge_graph import KnowledgeGraph

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BATCH = 500


def _ident(name: str, kind: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe {kind} name for a Cypher identifier: {name!r}")
    return name


def _chunks(seq: list, n: int) -> Iterator[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _node_props(node) -> dict[str, Any]:
    return {
        "name": node.name,
        "canonical_name": node.canonical_name,
        "namespace": node.namespace,
        "type": node.type,
        "attributes": json.dumps(node.attributes, ensure_ascii=False),
        "sources": sorted(node.sources),
        "provenance": json.dumps([p.to_dict() for p in node.provenance], ensure_ascii=False),
    }


def _edge_props(edge) -> dict[str, Any]:
    return {
        "relation": edge.relation,
        "attributes": json.dumps(edge.attributes, ensure_ascii=False),
        "sources": sorted({p.source_type for p in edge.provenance}),
        "provenance": json.dumps([p.to_dict() for p in edge.provenance], ensure_ascii=False),
    }


class Neo4jImportError(RuntimeError):
    """Raised when the driver is missing or the import cannot complete."""


def import_graph_to_neo4j(
    graph: KnowledgeGraph,
    *,
    uri: str,
    user: str,
    password: str,
    database: str = "neo4j",
) -> dict[str, Any]:
    """Load ``graph`` into Neo4j and return an import summary.

    Raises :class:`Neo4jImportError` if the driver is not installed or a statement
    fails (connection/auth errors surface with a clear message -- no silent partial
    success).
    """
    try:
        from neo4j import GraphDatabase  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise Neo4jImportError(
            "需要 neo4j 驱动：conda run -n CMMeter pip install \".[neo4j]\""
        ) from exc

    labels = sorted({n.type for n in graph.nodes})
    for lab in labels:
        _ident(lab, "label")

    summary: dict[str, Any] = {"uri": uri, "database": database, "nodes": 0, "edges": 0,
                               "labels": labels, "relations": []}
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
    except Exception as exc:  # noqa: BLE001
        raise Neo4jImportError(f"无法连接 Neo4j {uri}: {exc}") from exc

    try:
        with driver.session(database=database) as session:
            session.run("RETURN 1").consume()  # fail fast on bad auth / db

            # 1) id indexes per label (idempotent) -> speed up MERGE / MATCH.
            for lab in labels:
                session.run(
                    f"CREATE INDEX kg_idx_{_ident(lab, 'label')} IF NOT EXISTS "
                    f"FOR (n:`{lab}`) ON (n.id)"
                ).consume()

            # 2) nodes, grouped by label.
            by_label: dict[str, list] = defaultdict(list)
            for n in graph.nodes:
                by_label[n.type].append(n)
            for lab, nodes in by_label.items():
                for chunk in _chunks(nodes, _BATCH):
                    rows = [{"id": n.id, "props": _node_props(n)} for n in chunk]
                    session.run(
                        f"UNWIND $rows AS row MERGE (n:`{lab}` {{id: row.id}}) SET n += row.props",
                        rows=rows,
                    ).consume()
                    summary["nodes"] += len(chunk)

            # 3) edges, grouped by (src label, tgt label, relation) to use the indexes.
            type_of = {n.id: n.type for n in graph.nodes}
            by_shape: dict[tuple[str, str, str], list] = defaultdict(list)
            for e in graph.edges:
                st, tt = type_of.get(e.source), type_of.get(e.target)
                if not st or not tt:
                    continue  # endpoint not present; skip rather than emit a broken MATCH
                by_shape[(st, tt, _ident(e.relation, "relation type"))].append(e)
            for (st, tt, rel), edges in by_shape.items():
                for chunk in _chunks(edges, _BATCH):
                    rows = [{"s": e.source, "t": e.target, "props": _edge_props(e)} for e in chunk]
                    session.run(
                        f"UNWIND $rows AS row "
                        f"MATCH (a:`{st}` {{id: row.s}}), (b:`{tt}` {{id: row.t}}) "
                        f"MERGE (a)-[r:`{rel}`]->(b) SET r += row.props",
                        rows=rows,
                    ).consume()
                    summary["edges"] += len(chunk)
                summary["relations"].append(rel)
    except Neo4jImportError:
        raise
    except Exception as exc:  # noqa: BLE001 - any driver/DB error is fatal, not partial
        driver.close()
        raise Neo4jImportError(f"导入失败: {exc}") from exc
    finally:
        try:
            driver.close()
        except Exception:  # noqa: BLE001
            pass

    summary["relations"] = sorted(set(summary["relations"]))
    return summary
