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

    The open-source code collector (SCIP/Joern-based) is used by default.
    Set ``settings.code_analysis.enabled = False`` to disable code collection.
    """
    code_collector: BaseCollector
    if settings.code_analysis.enabled:
        code_collector = CodeCollector(settings)
    else:
        # Code collection disabled
        raise RuntimeError(
            "Code collection is disabled. "
            "Set code_analysis.enabled=true in config to enable code analysis."
        )
    return [
        code_collector,
        DocumentCollector(settings),
        DeploymentCollector(settings),
        RuntimeCollector(settings),
        HistoryCollector(settings),
    ]
