"""Collector registry -- the four Module-1 data sources in merge order.

Order matters only for readability; the pipeline adds all nodes before any edges
so cross-source merging is order-independent.
"""
from __future__ import annotations

from ..config import BuildSettings
from .base import BaseCollector, CollectorResult
from .code import CodeCollector
from .deployment import DeploymentCollector
from .document import DocumentCollector
from .history import HistoryCollector
from .runtime import RuntimeCollector

__all__ = [
    "BaseCollector",
    "CollectorResult",
    "CodeCollector",
    "DocumentCollector",
    "DeploymentCollector",
    "RuntimeCollector",
    "HistoryCollector",
    "build_collectors",
]


def build_collectors(settings: BuildSettings) -> list[BaseCollector]:
    """Instantiate all collectors (each decides via ``is_enabled()`` whether it runs).

    The open-source code collector (SCIP/Joern-based) is used when
    ``settings.code_analysis.enabled`` is True. When disabled, code collection
    is skipped entirely (no code nodes/edges in the graph).
    """
    result: list[BaseCollector] = []
    if settings.code_analysis.enabled:
        result.append(CodeCollector(settings))
    result.extend([
        DocumentCollector(settings),
        DeploymentCollector(settings),
        RuntimeCollector(settings),
        HistoryCollector(settings),
    ])
    return result
