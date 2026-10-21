"""Runtime collector (metrics / logs / traces) -- STUB.

Planned to read Prometheus metrics, centralized logs, and distributed traces,
then emit :class:`NodeType.METRIC` nodes and ``OBSERVED_BY`` relations onto the
services/deployments already in the graph, tagged with ``source=runtime``.

Unimplemented for now; the mapping path is written and tested so wiring a real
Prometheus/OTel client later is a drop-in for :meth:`_fetch_metrics`.
"""
from __future__ import annotations

from typing import Any

from ...config import BuildSettings
from ...graph.model import KGEdge, KGNode, Provenance, make_node_id
from ...graph.ontology import NodeType, RelationType, SourceType
from ..base import BaseCollector, CollectorResult


class RuntimeCollector(BaseCollector):
    name = "runtime"

    def __init__(self, settings: BuildSettings) -> None:
        self.settings = settings
        self.rt = settings.runtime
        self.namespace = settings.namespace

    def is_enabled(self) -> bool:
        return self.rt.enabled

    def collect(self) -> CollectorResult:
        result = CollectorResult(collector=self.name)
        metrics = self._fetch_metrics()
        if not metrics:
            result.warnings.append(
                "runtime collector is a stub: implement _fetch_metrics() to read from "
                "Prometheus / logs / traces."
            )
            return result
        for m in metrics:
            self._map_metric(result, m)
        result.metrics["metrics_ingested"] = len(metrics)
        return result

    def _fetch_metrics(self) -> list[dict[str, Any]]:
        """TODO(Module 1 follow-up): query ``self.rt.prometheus_url`` / trace backend.

        Each record is expected to look like::

            {"service": "Order", "name": "p99_latency_ms", "value": 812, "window": "5m"}
        """
        return []

    def _map_metric(self, result: CollectorResult, m: dict[str, Any]) -> None:
        name = m.get("name", "metric")
        svc = m.get("service") or "unknown"
        prov = self._prov(f"{svc}/{name}", str(m))
        metric_id = make_node_id(NodeType.METRIC.value, f"{svc}/{name}", self.namespace)
        result.add_node(KGNode(
            id=metric_id, type=NodeType.METRIC.value, name=name, canonical_name=f"{svc}/{name}",
            namespace=self.namespace,
            attributes={"value": m.get("value"), "window": m.get("window"), "service": svc},
            provenance=[prov],
        ))
        svc_id = make_node_id(NodeType.SERVICE.value, svc, self.namespace)
        result.add_node(KGNode(
            id=svc_id, type=NodeType.SERVICE.value, name=svc, canonical_name=svc,
            namespace=self.namespace, attributes={}, provenance=[prov],
        ))
        result.add_edge(KGEdge(svc_id, metric_id, RelationType.OBSERVED_BY.value,
                               {"value": m.get("value")}, [prov]))

    def _prov(self, source_id: str, evidence: str) -> Provenance:
        return Provenance(
            source_type=SourceType.RUNTIME,
            source_id=source_id,
            evidence=evidence,
            confidence=0.9,
            detail={"collector": self.name, "status": "stub"},
        )
