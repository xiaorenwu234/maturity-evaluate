"""Deployment collector (Kubernetes / cloud) -- STUB.

As requested, actual cloud/K8s data collection is left unimplemented for now.
The class is fully wired to the rest of the pipeline, however, so filling it in
later is a matter of implementing :meth:`_fetch_workloads` against a real
Kubernetes client. Everything downstream (node/edge mapping, provenance tagging
with ``source=deployment``) is already written and exercised by unit tests via a
fixture, so the evidence-chain behaviour matches the spec, e.g.::

    Payment --DEPLOYED_AS--> Deployment(order/payment)
    Deployment(order/payment) --replica--> 1     source: Kubernetes

Data it is meant to surface per the design:
    Service, Deployment, Pod, Replica, CPU/Memory Limit, HPA,
    Readiness Probe, Liveness Probe, Region / AZ.
"""
from __future__ import annotations

from typing import Any

from ...config import BuildSettings
from ...graph.model import KGEdge, KGNode, Provenance, make_node_id
from ...graph.ontology import NodeType, RelationType, SourceType
from ..base import BaseCollector, CollectorResult


class DeploymentCollector(BaseCollector):
    name = "deployment"

    def __init__(self, settings: BuildSettings) -> None:
        self.settings = settings
        self.dep = settings.deployment
        self.namespace = settings.namespace

    def is_enabled(self) -> bool:
        # Disabled by default -- flip on once _fetch_workloads is implemented.
        return self.dep.enabled

    # ------------------------------------------------------------------ run
    def collect(self) -> CollectorResult:
        result = CollectorResult(collector=self.name)
        workloads = self._fetch_workloads()
        if not workloads:
            result.warnings.append(
                "deployment collector is a stub: no live cluster connection configured. "
                "Implement _fetch_workloads() to read from the Kubernetes API."
            )
            return result
        for wl in workloads:
            self._map_workload(result, wl)
        return result

    # ---------------------------------------------- FUTURE: real K8s fetching
    def _fetch_workloads(self) -> list[dict[str, Any]]:
        """Return normalised workload records from the cluster.

        TODO(Module 1 follow-up): connect the ``kubernetes`` client using
        ``self.settings.deployment`` (kubeconfig / context / namespaces) and
        return one dict per Deployment/StatefulSet shaped like::

            {
              "service": "Payment",
              "name": "payment",
              "namespace": "prod",
              "cluster": self.dep.cluster_name,
              "replicas": 1,
              "cpu_limit": "500m",
              "memory_limit": "512Mi",
              "hpa": None,                 # or {"min":..,"max":..,"target":..}
              "readiness_probe": False,
              "liveness_probe": False,
              "region": "cn-north-1a",
            }

        For now this returns an empty list (stub). Tests inject fixture records
        straight into :meth:`_map_workload`.
        """
        return []

    # ------------------------------------------------------------- mapping
    def _map_workload(self, result: CollectorResult, wl: dict[str, Any]) -> None:
        svc_name = wl.get("service") or wl.get("name") or "unknown"
        ns = wl.get("namespace", "")
        cluster = wl.get("cluster", self.dep.cluster_name)
        prov = self._prov(f"{cluster}/{ns}/deployment/{wl.get('name', svc_name)}", str(wl))

        dep_id = make_node_id(NodeType.DEPLOYMENT.value, f"{ns}/{wl.get('name', svc_name)}", self.namespace)
        result.add_node(KGNode(
            id=dep_id, type=NodeType.DEPLOYMENT.value, name=wl.get("name", svc_name),
            canonical_name=f"{ns}/{wl.get('name', svc_name)}", namespace=self.namespace,
            attributes={
                "cluster": cluster, "namespace": ns,
                "replicas": wl.get("replicas"),
                "cpu_limit": wl.get("cpu_limit"),
                "memory_limit": wl.get("memory_limit"),
                "hpa": wl.get("hpa"),
                "readiness_probe": wl.get("readiness_probe"),
                "liveness_probe": wl.get("liveness_probe"),
                "region": wl.get("region"),
            },
            provenance=[prov],
        ))

        svc_id = make_node_id(NodeType.SERVICE.value, svc_name, self.namespace)
        # Create the service node too if deployment is the only source that saw it.
        result.add_node(KGNode(
            id=svc_id, type=NodeType.SERVICE.value, name=svc_name, canonical_name=svc_name,
            namespace=self.namespace, attributes={"seen_in_cluster": True}, provenance=[prov],
        ))
        result.add_edge(KGEdge(svc_id, dep_id, RelationType.DEPLOYED_AS.value,
                               {"replicas": wl.get("replicas")}, [prov]))

    def _prov(self, source_id: str, evidence: Any) -> Provenance:
        return Provenance(
            source_type=SourceType.DEPLOYMENT,
            source_id=source_id,
            evidence=str(evidence),
            confidence=1.0,
            detail={"collector": self.name, "cluster": self.dep.cluster_name, "status": "stub"},
        )
