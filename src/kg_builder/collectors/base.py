"""Collector contract shared by all Module 1 data sources.

A collector reads *one* kind of input (source code / documents / deployment /
runtime) and emits a :class:`CollectorResult` -- a bag of graph nodes and edges,
each already annotated with :class:`Provenance`. The build pipeline merges all
results into the single :class:`KnowledgeGraph`.

Keeping collectors producing the *same* primitives (KGNode / KGEdge) is what lets
code, docs, deployment and runtime facts land on the same logical node and build
a cross-source evidence chain.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..graph.model import KGEdge, KGNode


@dataclass
class CollectorResult:
    """Nodes + edges produced by one collector run."""

    collector: str
    nodes: list[KGNode] = field(default_factory=list)
    edges: list[KGEdge] = field(default_factory=list)
    # Free-form run metrics (files scanned, parse failures, ...).
    metrics: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Auxiliary, non-graph payloads (e.g. statement-level CFGs, clone groups).
    # Kept out of the graph to avoid bloating it; the pipeline exports / folds them
    # into graph-level meta as appropriate.
    extras: dict = field(default_factory=dict)

    def add_node(self, node: KGNode) -> None:
        self.nodes.append(node)

    def add_edge(self, edge: KGEdge) -> None:
        self.edges.append(edge)


class BaseCollector(ABC):
    """Interface every collector implements."""

    #: Short identifier, e.g. "code", "document". Overridden by subclasses.
    name: str = "base"

    @abstractmethod
    def collect(self) -> CollectorResult:
        """Run the collector and return its result. Must not raise on partial
        input failures -- record them in ``CollectorResult.warnings``."""

    def is_enabled(self) -> bool:  # pragma: no cover - trivial default
        return True
