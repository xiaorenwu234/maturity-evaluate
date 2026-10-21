"""Build pipeline: the orchestration that produces the Unified System Knowledge Graph.

Flow::

    for each enabled collector -> CollectorResult (nodes + edges + provenance)
    merge all nodes into KnowledgeGraph   (nodes before edges!)
    merge all edges into KnowledgeGraph   (provenance accumulates per fact)
    run SemanticEnricher                  (LLM-only; skippable via settings.enrich)
    persist JSON + optional mermaid/cypher

This graph is Module 1's *only* output and the *only* data entry for Module 2/3.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..collectors import BaseCollector, CollectorResult, build_collectors
from ..config import BuildSettings
from ..enrichment import SemanticEnricher, create_llm_client
from ..graph.knowledge_graph import KnowledgeGraph
from ..graph.model import KGEdge, KGNode
from ..graph.serialization import save_graph, to_cypher, to_mermaid

logger = logging.getLogger("kg_builder.pipeline")


@dataclass
class BuildReport:
    graph: KnowledgeGraph
    results: list[CollectorResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    enrichment: dict[str, Any] = field(default_factory=dict)
    output_path: Optional[Path] = None

    @property
    def stats(self) -> dict[str, Any]:
        return self.graph.stats()

    def summary_text(self) -> str:
        s = self.stats
        lines = [
            "=== Unified System Knowledge Graph ===",
            f"nodes: {s['node_count']}  edges: {s['edge_count']}",
            "nodes by type:",
        ]
        for k, v in sorted(s["nodes_by_type"].items()):
            lines.append(f"  - {k:18s} {v}")
        lines.append("relations by type:")
        for k, v in sorted(s["relations_by_type"].items()):
            lines.append(f"  - {k:18s} {v}")
        lines.append("provenance by source:")
        for k, v in sorted(s["provenance_by_source"].items()):
            lines.append(f"  - {k:18s} {v}")
        if self.enrichment:
            lines.append("semantic enrichment:")
            for k, v in self.enrichment.items():
                lines.append(f"  - {k}: {v}")
        if self.warnings:
            lines.append(f"warnings: {len(self.warnings)}")
            for w in self.warnings[:10]:
                lines.append(f"  ! {w}")
        if self.output_path:
            lines.append(f"written to: {self.output_path}")
        return "\n".join(lines)


class BuildPipeline:
    def __init__(self, settings: BuildSettings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ run
    def run(
        self,
        collectors: Optional[list[BaseCollector]] = None,
        write_outputs: bool = True,
        llm_client=None,
    ) -> BuildReport:
        collectors = collectors if collectors is not None else build_collectors(self.settings)
        results: list[CollectorResult] = []
        warnings: list[str] = []

        for c in collectors:
            if not c.is_enabled():
                logger.info("collector %s disabled, skipping", c.name)
                continue
            logger.info("running collector: %s", c.name)
            res = c.collect()
            results.append(res)
            warnings.extend(f"[{c.name}] {w}" for w in res.warnings)

        graph = self._merge(results)
        cfg_payload = self._fold_extras(graph, results)
        report = self._enrich(graph, results, warnings, llm_client)

        if write_outputs:
            report.output_path = self._write(graph, report, cfg_payload)
        return report

    # ---------------------------------------------------------------- merge
    @staticmethod
    def _merge(results: list[CollectorResult]) -> KnowledgeGraph:
        graph = KnowledgeGraph()
        # 1) nodes first so edges can always find endpoints
        for r in results:
            for n in r.nodes:
                graph.add_node(_clone_node(n))
        # 2) then edges
        for r in results:
            for e in r.edges:
                try:
                    graph.add_edge(_clone_edge(e))
                except KeyError as exc:
                    logger.warning("dropped edge with missing endpoint: %s", exc)
        return graph

    # --------------------------------------------------------------- enrich
    def _enrich(
        self,
        graph: KnowledgeGraph,
        results: list[CollectorResult],
        warnings: list[str],
        llm_client=None,
    ) -> BuildReport:
        # Enrichment is LLM-only. When disabled we build the structural graph
        # alone; when enabled the LLM must be configured (create_llm_client raises
        # otherwise) -- there is no heuristic fallback.
        if not self.settings.enrich:
            summary: dict = {"skipped": True}
        else:
            llm = llm_client if llm_client is not None else create_llm_client(self.settings.llm)
            enricher = SemanticEnricher(
                llm=llm,
                namespace=self.settings.namespace,
                max_items_per_call=self.settings.llm.max_items_per_call,
            )
            summary = enricher.enrich(graph)
        return BuildReport(graph=graph, results=results, warnings=warnings, enrichment=summary)

    # ---------------------------------------------------------- fold extras
    @staticmethod
    def _fold_extras(graph: KnowledgeGraph, results: list[CollectorResult]) -> Optional[dict]:
        """Fold collectors' auxiliary ``extras`` into graph meta + a CFG payload.

        Clone-group statistics become document-level ``graph.meta`` (so Module 2
        sees duplicate-code risk without walking every edge); statement-level CFGs
        are returned as a side-car payload for a separate ``.cfg.json`` export --
        keeping the main graph lean.
        """
        clone_groups: list[dict] = []
        cfg: dict = {}
        for r in results:
            ex = r.extras or {}
            clone_groups.extend(ex.get("clone_groups", []))
            cfg.update(ex.get("cfg", {}))
        if clone_groups:
            clone_groups.sort(key=lambda g: (g.get("size", 0), g.get("cross_file", False)), reverse=True)
            graph.meta["clone_analysis"] = {
                "clone_group_count": len(clone_groups),
                "cross_file_groups": sum(1 for g in clone_groups if g.get("cross_file")),
                "cloned_functions": sum(g.get("size", 0) for g in clone_groups),
                "groups": clone_groups,
            }
        return {"functions": cfg} if cfg else None

    # ---------------------------------------------------------------- write
    def _write(self, graph: KnowledgeGraph, report: BuildReport, cfg_payload: Optional[dict] = None) -> Path:
        out = Path(self.settings.output_path)
        saved = save_graph(graph, out)
        # Side-car visualisations (best-effort; never fail the build for these).
        try:
            out.with_suffix(".mmd").write_text(to_mermaid(graph), encoding="utf-8")
            out.with_suffix(".cypher").write_text(to_cypher(graph), encoding="utf-8")
            if cfg_payload and cfg_payload.get("functions"):
                import json

                (out.parent / f"{out.stem}.cfg.json").write_text(
                    json.dumps(cfg_payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except OSError:
            pass
        return saved


# ------------------------------------------------------------- helpers
def _clone_node(n: KGNode) -> KGNode:
    """Return a node sharing identity but with an independent provenance list,
    so merging into the graph never mutates the collector's own result objects."""
    clone = KGNode(
        id=n.id, type=n.type, name=n.name, canonical_name=n.canonical_name,
        namespace=n.namespace, attributes=dict(n.attributes), provenance=list(n.provenance),
    )
    return clone


def _clone_edge(e: KGEdge) -> KGEdge:
    clone = KGEdge(
        source=e.source, target=e.target, relation=e.relation,
        attributes=dict(e.attributes), provenance=list(e.provenance),
    )
    return clone
