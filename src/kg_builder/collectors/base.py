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

# Java source roots stripped before deriving a module name.
_JAVA_SOURCE_ROOTS = ("src/main/java/", "src/test/java/", "src/")


def python_module_name(rel_path: str) -> str:
    """Derive the dotted module name a Python file contributes to the graph.

    ``services/order_service.py`` -> ``services.order_service`` and
    ``pkg/__init__.py`` -> ``pkg``. This is the canonical key the code collector
    stores on ``Module`` nodes, so history/document facts can merge onto the same
    node instead of creating variants.
    """
    stem = rel_path.rsplit(".", 1)[0]
    parts = [p for p in stem.replace("\\", "/").split("/") if p not in {"", "."}]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or rel_path


def java_module_name(rel_path: str) -> str:
    """Derive the module name a Java file contributes to the graph.

    ``src/main/java/com/example/OrderService.java`` -> ``com.example.OrderService``.
    Keeping the file name in the key makes ``FileArtifact.module`` identify one
    file (the class qname is ``com.example.OrderService``); the code collector
    uses this same function so both sides agree.
    """
    p = rel_path.replace("\\", "/")
    for prefix in _JAVA_SOURCE_ROOTS:
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    return p.removesuffix(".java").replace("/", ".")


def module_name_of(rel_path: str) -> str:
    """Module key for any source path (dispatches on the file extension)."""
    if rel_path.replace("\\", "/").lower().endswith(".java"):
        return java_module_name(rel_path)
    return python_module_name(rel_path)


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
