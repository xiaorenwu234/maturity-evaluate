"""Entity resolution over the merged graph.

Different sources name the same thing differently: code declares the class
``OrderService``, the Kubernetes manifest its workload ``order-service`` and the
architecture doc writes ``Order Service``. Likewise an HTTP endpoint may be
coded as ``POST /order/{id}`` but documented as ``post /order/:id``. Because
node ids are derived from the canonical name, those variants would otherwise
stay *separate* nodes and cross-source edges (``Service --DEPLOYED_AS-->
Deployment``, ``Document --MENTIONS--> API``) would point at unrelated entities.

Resolution folds Service nodes whose names differ only in case/separators
(``service_key``) and API nodes whose verb matches but whose path spelling
drifts (``api_key``). Beyond that it stays conservative on purpose: it never
guesses that ``Order`` and ``OrderService`` are the same thing, and it keeps
``GET /x`` distinct from ``POST /x`` -- an unsafe merge destroys the evidence
chain Module 2 depends on.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .knowledge_graph import KnowledgeGraph
from .model import KGNode
from .ontology import NodeType, SourceType

#: Strongest evidence wins when choosing which node keeps the id.
_SOURCE_PRIORITY = {
    SourceType.SOURCE_CODE.value: 0,
    SourceType.DEPLOYMENT.value: 1,
    SourceType.DOCUMENT.value: 2,
    SourceType.RUNTIME.value: 3,
    SourceType.HISTORY.value: 4,
    SourceType.MANUAL.value: 5,
    SourceType.LLM.value: 6,
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def service_key(name: str) -> str:
    """Normalised merge key: ``OrderService`` / ``order_service`` / ``order-service``.

    Only case and separators are folded; words are never stems or dropped, so
    ``OrderService`` and ``OrderGateway`` keep distinct keys.
    """
    return _NON_ALNUM.sub("", (name or "").lower())


#: A path segment that is a route parameter, in either ``{id}`` or ``:id`` form.
_API_TEMPLATE_SEG = re.compile(r"^\{.*\}$|^:.*$")


def api_key(name: str) -> str:
    """Normalised merge key for an API node: ``GET /order/{id}`` -> ``get /order/*``.

    Folds HTTP-verb case, route-parameter spelling (``{id}`` vs ``:id``) and
    trailing slashes so a doc/LLM spelling reconciles with the code-derived
    endpoint. The verb itself is preserved -- ``GET /x`` and ``POST /x`` are
    distinct REST operations and must never collapse into one node.
    """
    s = (name or "").strip().lower()
    if not s:
        return ""
    method, _, path = s.partition(" ")
    if not path:                      # no verb recorded; whole string is a path
        method, path = "", method
    segs = [("*" if _API_TEMPLATE_SEG.match(seg) else seg)
            for seg in path.split("/") if seg]
    return f"{method} /{'/'.join(segs)}" if segs else method


def _representative_rank(node: KGNode) -> tuple[int, int, int]:
    """Lower is better: strongest source, then most evidence, then shortest name."""
    best_source = min(
        (_SOURCE_PRIORITY.get(p.source_type, 9) for p in node.provenance), default=9
    )
    return (best_source, -len(node.provenance), len(node.name))


def _merge_type_by_key(
    graph: KnowledgeGraph, type_value: str, key_fn: Callable[[str], str],
    merged_groups: list[dict[str, Any]],
) -> int:
    """Fold nodes of one type that share a normalised key. Returns merge count."""
    groups: dict[str, list[KGNode]] = {}
    for node in graph.nodes:
        if node.type != type_value:
            continue
        key = key_fn(node.canonical_name or node.name)
        if key:
            groups.setdefault(key, []).append(node)

    count = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        primary = min(members, key=_representative_rank)
        aliases = sorted({m.name for m in members if m.name != primary.name})
        graph.merge_nodes(primary.id, [m.id for m in members if m.id != primary.id])
        if aliases:
            known = set(primary.attributes.get("aliases", []))
            primary.attributes["aliases"] = sorted(known | set(aliases))
        primary.attributes.setdefault("resolved_key", key)
        count += len(members) - 1
        merged_groups.append({
            "type": type_value,
            "key": key,
            "primary": primary.name,
            "merged": aliases,
            "sources": sorted({p.source_type for p in primary.provenance}),
        })
    return count


def resolve_entities(graph: KnowledgeGraph) -> dict[str, Any]:
    """Merge name variants of the same Service / API node in place.

    Returns a summary (also meant for ``graph.meta["entity_resolution"]``) so the
    build report shows exactly what was folded together.
    """
    merged_groups: list[dict[str, Any]] = []
    merged_count = _merge_type_by_key(graph, NodeType.SERVICE.value, service_key, merged_groups)
    merged_count += _merge_type_by_key(graph, NodeType.API.value, api_key, merged_groups)

    merged_groups.sort(key=lambda g: (g["type"], g["key"]))
    return {"merged_nodes": merged_count, "groups": merged_groups}
