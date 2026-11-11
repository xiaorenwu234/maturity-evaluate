"""Base classes for document config parsers.

Each parser in ``document/utils/`` inherits :class:`BaseConfigParser` and
implements two methods:

* :meth:`can_parse` -- fast, rule-based check (no LLM) to decide whether a
  file belongs to this parser's domain.
* :meth:`parse` -- extract graph nodes and edges from the file content.

The orchestrator (:func:`find_parser`) walks the registry and returns the
first parser whose ``can_parse`` returns True.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ....graph.model import KGEdge, KGNode, Provenance, make_node_id
from ....graph.ontology import NodeType


def owner_service_node(
    owner_service: str,
    namespace: str,
    rel_path: str,
    prov: Provenance,
) -> KGNode:
    """Build the Service node a parser is about to attach an edge to.

    Parsers used to emit ``Service --X--> resource`` edges without emitting the
    Service node itself, and the build pipeline then dropped those edges as
    dangling. Emitting the endpoint keeps the relation.
    """
    return KGNode(
        id=make_node_id(NodeType.SERVICE.value, owner_service, namespace),
        type=NodeType.SERVICE.value,
        name=owner_service,
        canonical_name=owner_service,
        namespace=namespace,
        attributes={"declared_in": rel_path},
        provenance=[prov],
    )


@dataclass
class ParseResult:
    """Output of a single config parser invocation."""

    nodes: list[KGNode] = field(default_factory=list)
    edges: list[KGEdge] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class BaseConfigParser(ABC):
    """Abstract base for domain-specific config file parsers.

    Subclasses must implement:
    - ``can_parse(rel_path, ext, content) -> bool``
    - ``parse(rel_path, content, owner_service, namespace) -> ParseResult``
    """

    # Human-readable category key used by LLM classification routing.
    # Must match one of the keys in PARSER_REGISTRY.
    category: str = ""

    @staticmethod
    @abstractmethod
    def can_parse(rel_path: str, ext: str, content: str) -> bool:
        """Return True if this parser can handle the given file.

        This must be a fast, deterministic check -- no LLM calls.
        Typically inspects file extension, path patterns, or content headers.
        """

    @abstractmethod
    def parse(
        self,
        rel_path: str,
        content: str,
        owner_service: Optional[str],
        namespace: str,
    ) -> ParseResult:
        """Parse file content and return graph nodes + edges.

        Args:
            rel_path: Relative path within the repo (e.g. "k8s/order-service.yaml").
            content: Raw file text.
            owner_service: Service name determined by LLM (may be None).
            namespace: Graph namespace for node ID generation.
        """
