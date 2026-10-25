"""Deployment collector -- unified entry point for infrastructure data sources.

Orchestrates multiple sub-collectors (K8s, FaaS, etc.) and merges their results
into a single ``CollectorResult``. Each sub-collector lives in its own sub-package
and returns ``(nodes, edges, warnings)`` tuples.

To add a new infrastructure source:
1. Create a new sub-package (e.g. ``faas/``)
2. Implement a collector class with ``collect() -> (nodes, edges, warnings)``
3. Register it in ``_SUB_COLLECTORS`` below
"""
from __future__ import annotations

import logging
from typing import Callable

from ...config import BuildSettings
from ..base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)


class DeploymentCollector(BaseCollector):
    name = "deployment"

    def __init__(self, settings: BuildSettings) -> None:
        self.settings = settings
        self.dep = settings.deployment

    def is_enabled(self) -> bool:
        return self.dep.enabled

    def collect(self) -> CollectorResult:
        result = CollectorResult(collector=self.name)

        if not self.dep.enabled:
            return result

        # Registry of sub-collectors: (name, factory_function)
        # Each factory receives (dep_settings, graph_namespace) and returns
        # an object with .collect() -> (nodes, edges, warnings).
        sub_collectors = self._get_sub_collectors()

        collected: list[str] = []
        for sc_name, factory in sub_collectors:
            try:
                sub = factory(self.dep, self.settings.namespace)
                nodes, edges, warnings = sub.collect()
                for n in nodes:
                    result.add_node(n)
                for e in edges:
                    result.add_edge(e)
                result.warnings.extend(warnings)
                if nodes or edges:
                    collected.append(sc_name)
                    logger.info(f"deployment.{sc_name}: {len(nodes)} nodes, {len(edges)} edges")
                elif warnings:
                    logger.warning(f"deployment.{sc_name}: {warnings}")
            except Exception as exc:
                msg = f"deployment.{sc_name}: failed ({exc})"
                result.warnings.append(msg)
                logger.error(msg, exc_info=True)

        result.metrics["sub_collectors"] = collected
        return result

    @staticmethod
    def _get_sub_collectors() -> list[tuple[str, Callable]]:
        """Return the list of registered sub-collectors.

        Add new infrastructure sources here. Each entry is a tuple of
        (name, factory) where factory(dep_settings, namespace) creates
        the sub-collector instance.
        """
        collectors = []

        # K8s is always available when deployment is enabled
        from .k8s import K8sCollector
        collectors.append(("k8s", K8sCollector))

        # Future sub-collectors can be added here, e.g.:
        # from .faas import FaaSCollector
        # collectors.append(("faas", FaaSCollector))

        return collectors
