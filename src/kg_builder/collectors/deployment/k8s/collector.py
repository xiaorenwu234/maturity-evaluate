"""Kubernetes deployment collector.

Connects to a K8s cluster via kubeconfig and collects:
- Deployments / StatefulSets / DaemonSets
- Pods (with resource requests/limits, probes, QoS)
- Services / Ingresses
- HPA / VPA
- Nodes
- Namespaces / ResourceQuotas

Each resource is mapped to KGNode/KGEdge with source=deployment provenance.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ....config import DeploymentSettings
from ....graph.model import KGEdge, KGNode, Provenance, make_node_id
from ....graph.ontology import NodeType, RelationType, SourceType
from ...base import CollectorResult

logger = logging.getLogger(__name__)


class K8sCollector:
    """Kubernetes sub-collector.

    Connects to a K8s cluster via kubeconfig and collects workloads,
    pods, services, ingresses, HPA, nodes, and namespaces.
    Returns (nodes, edges, warnings) tuple for the parent orchestrator.
    """
    name = "k8s"

    def __init__(self, dep_settings: DeploymentSettings, graph_namespace: str = "") -> None:
        self.dep = dep_settings
        self.namespace = graph_namespace

    def collect(self) -> tuple[list[KGNode], list[KGEdge], list[str]]:
        nodes: list[KGNode] = []
        edges: list[KGEdge] = []
        warnings: list[str] = []

        try:
            from kubernetes import client, config as k8s_config
        except ImportError:
            warnings.append(
                "kubernetes package not installed. Install with: pip install -e '.[k8s]'"
            )
            return nodes, edges, warnings

        # Load kubeconfig
        try:
            if self.dep.kubeconfig:
                k8s_config.load_kube_config(
                    config_file=self.dep.kubeconfig,
                    context=self.dep.context,
                )
            else:
                k8s_config.load_incluster_config()
        except Exception as exc:
            warnings.append(f"Failed to load kubeconfig: {exc}")
            return nodes, edges, warnings

        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()
        autoscaling_v1 = client.AutoscalingV1Api()

        namespaces = self.dep.namespaces or self._list_namespaces(v1)
        cluster = self.dep.cluster_name

        logger.info(f"K8s collector: scanning {len(namespaces)} namespace(s) in cluster '{cluster}'")

        for ns in namespaces:
            self._collect_namespace(nodes, edges, v1, ns, cluster)
            self._collect_deployments(nodes, edges, apps_v1, ns, cluster)
            self._collect_statefulsets(nodes, edges, apps_v1, ns, cluster)
            self._collect_pods(nodes, edges, v1, ns, cluster)
            self._collect_services(nodes, edges, v1, ns, cluster)
            self._collect_ingresses(nodes, edges, v1, ns, cluster)
            self._collect_hpa(nodes, edges, autoscaling_v1, ns, cluster)

        # Nodes are cluster-scoped
        self._collect_nodes(nodes, edges, v1, cluster)

        return nodes, edges, warnings

    # ---------------------------------------------------------------------------
    # Namespace listing
    # ---------------------------------------------------------------------------

    @staticmethod
    def _list_namespaces(v1) -> list[str]:
        try:
            ns_list = v1.list_namespace()
            return [ns.metadata.name for ns in ns_list.items]
        except Exception as exc:
            logger.warning(f"Failed to list namespaces: {exc}")
            return ["default"]

    # ---------------------------------------------------------------------------
    # Collectors per resource type
    # ---------------------------------------------------------------------------

    def _collect_namespace(self, nodes: list, edges: list, v1, ns: str, cluster: str):
        prov = self._prov(f"{cluster}/namespace/{ns}", f"namespace {ns}")
        node_id = make_node_id("Namespace", ns, self.namespace)
        nodes.append(KGNode(
            id=node_id, type="Namespace", name=ns, canonical_name=ns,
            namespace=self.namespace,
            attributes={"cluster": cluster},
            provenance=[prov],
        ))

    def _collect_deployments(self, nodes: list, edges: list, apps_v1, ns: str, cluster: str):
        try:
            deps = apps_v1.list_namespaced_deployment(ns)
        except Exception as exc:
            logger.warning(f"Failed to list deployments in {ns}: {exc}")
            return

        for dep in deps.items:
            meta = dep.metadata
            spec = dep.spec
            status = dep.status
            prov = self._prov(f"{cluster}/{ns}/deployment/{meta.name}", str(meta.name))

            dep_id = make_node_id(NodeType.DEPLOYMENT.value, f"{ns}/{meta.name}", self.namespace)
            nodes.append(KGNode(
                id=dep_id, type=NodeType.DEPLOYMENT.value, name=meta.name,
                canonical_name=f"{ns}/{meta.name}", namespace=self.namespace,
                attributes={
                    "cluster": cluster, "namespace": ns,
                    "replicas": spec.replicas,
                    "ready_replicas": status.ready_replicas,
                    "strategy": spec.strategy.type if spec.strategy else None,
                    "labels": dict(meta.labels or {}),
                },
                provenance=[prov],
            ))

            # Link to service if labels match (heuristic: same name)
            svc_id = make_node_id(NodeType.SERVICE.value, meta.name, self.namespace)
            edges.append(KGEdge(svc_id, dep_id, RelationType.DEPLOYED_AS.value,
                                   {"replicas": spec.replicas}, [prov]))

    def _collect_statefulsets(self, nodes: list, edges: list, apps_v1, ns: str, cluster: str):
        try:
            sts_list = apps_v1.list_namespaced_stateful_set(ns)
        except Exception as exc:
            logger.warning(f"Failed to list statefulsets in {ns}: {exc}")
            return

        for sts in sts_list.items:
            meta = sts.metadata
            spec = sts.spec
            prov = self._prov(f"{cluster}/{ns}/statefulset/{meta.name}", str(meta.name))

            sts_id = make_node_id(NodeType.DEPLOYMENT.value, f"{ns}/{meta.name}", self.namespace)
            nodes.append(KGNode(
                id=sts_id, type=NodeType.DEPLOYMENT.value, name=meta.name,
                canonical_name=f"{ns}/{meta.name}", namespace=self.namespace,
                attributes={
                    "cluster": cluster, "namespace": ns,
                    "kind": "StatefulSet",
                    "replicas": spec.replicas,
                    "service_name": spec.service_name,
                    "labels": dict(meta.labels or {}),
                },
                provenance=[prov],
            ))

    def _collect_pods(self, nodes: list, edges: list, v1, ns: str, cluster: str):
        try:
            pods = v1.list_namespaced_pod(ns)
        except Exception as exc:
            logger.warning(f"Failed to list pods in {ns}: {exc}")
            return

        for pod in pods.items:
            meta = pod.metadata
            spec = pod.spec
            status = pod.status
            prov = self._prov(f"{cluster}/{ns}/pod/{meta.name}", str(meta.name))

            # Extract resource requests/limits from first container
            resources: dict[str, Any] = {}
            readiness_probe = False
            liveness_probe = False
            if spec.containers:
                c0 = spec.containers[0]
                if c0.resources:
                    if c0.resources.requests:
                        resources["requests"] = dict(c0.resources.requests)
                    if c0.resources.limits:
                        resources["limits"] = dict(c0.resources.limits)
                readiness_probe = c0.readiness_probe is not None
                liveness_probe = c0.liveness_probe is not None

            pod_id = make_node_id("Pod", f"{ns}/{meta.name}", self.namespace)
            nodes.append(KGNode(
                id=pod_id, type="Pod", name=meta.name,
                canonical_name=f"{ns}/{meta.name}", namespace=self.namespace,
                attributes={
                    "cluster": cluster, "namespace": ns,
                    "status": status.phase,
                    "node": spec.node_name,
                    "restart_count": sum(cs.restart_count for cs in (status.container_statuses or [])),
                    "qos_class": status.qos_class,
                    "resources": resources,
                    "readiness_probe": readiness_probe,
                    "liveness_probe": liveness_probe,
                    "labels": dict(meta.labels or {}),
                },
                provenance=[prov],
            ))

    def _collect_services(self, nodes: list, edges: list, v1, ns: str, cluster: str):
        try:
            svcs = v1.list_namespaced_service(ns)
        except Exception as exc:
            logger.warning(f"Failed to list services in {ns}: {exc}")
            return

        for svc in svcs.items:
            meta = svc.metadata
            spec = svc.spec
            prov = self._prov(f"{cluster}/{ns}/service/{meta.name}", str(meta.name))

            svc_id = make_node_id(NodeType.SERVICE.value, meta.name, self.namespace)
            ports = []
            if spec.ports:
                ports = [{"port": p.port, "target_port": p.target_port, "protocol": p.protocol}
                         for p in spec.ports]

            nodes.append(KGNode(
                id=svc_id, type=NodeType.SERVICE.value, name=meta.name,
                canonical_name=meta.name, namespace=self.namespace,
                attributes={
                    "cluster": cluster, "namespace": ns,
                    "service_type": spec.type,
                    "cluster_ip": spec.cluster_ip,
                    "ports": ports,
                    "labels": dict(meta.labels or {}),
                },
                provenance=[prov],
            ))

    def _collect_ingresses(self, nodes: list, edges: list, v1, ns: str, cluster: str):
        try:
            ings = v1.list_namespaced_ingress(ns)
        except Exception:
            return  # Ingress API may not be available

        for ing in ings.items:
            meta = ing.metadata
            prov = self._prov(f"{cluster}/{ns}/ingress/{meta.name}", str(meta.name))

            rules = []
            if ing.spec.rules:
                for rule in ing.spec.rules:
                    paths = []
                    if rule.http and rule.http.paths:
                        paths = [{"path": p.path, "backend": str(p.backend)} for p in rule.http.paths]
                    rules.append({"host": rule.host, "paths": paths})

            ing_id = make_node_id(NodeType.API.value, f"{ns}/{meta.name}", self.namespace)
            nodes.append(KGNode(
                id=ing_id, type=NodeType.API.value, name=meta.name,
                canonical_name=f"{ns}/{meta.name}", namespace=self.namespace,
                attributes={
                    "cluster": cluster, "namespace": ns,
                    "rules": rules,
                    "labels": dict(meta.labels or {}),
                },
                provenance=[prov],
            ))

    def _collect_hpa(self, nodes: list, edges: list, autoscaling_v1, ns: str, cluster: str):
        try:
            hpas = autoscaling_v1.list_namespaced_horizontal_pod_autoscaler(ns)
        except Exception:
            return

        for hpa in hpas.items:
            meta = hpa.metadata
            spec = hpa.spec
            prov = self._prov(f"{cluster}/{ns}/hpa/{meta.name}", str(meta.name))

            hpa_id = make_node_id("AutoScaling", f"{ns}/{meta.name}", self.namespace)
            nodes.append(KGNode(
                id=hpa_id, type="AutoScaling", name=meta.name,
                canonical_name=f"{ns}/{meta.name}", namespace=self.namespace,
                attributes={
                    "cluster": cluster, "namespace": ns,
                    "min_replicas": spec.min_replicas,
                    "max_replicas": spec.max_replicas,
                    "target_cpu_utilization": spec.target_cpu_utilization_percentage,
                    "scale_target": spec.scale_target_ref.name if spec.scale_target_ref else None,
                },
                provenance=[prov],
            ))

            # Edge: HPA scales a deployment
            if spec.scale_target_ref:
                target_id = make_node_id(NodeType.DEPLOYMENT.value,
                                         f"{ns}/{spec.scale_target_ref.name}", self.namespace)
                edges.append(KGEdge(hpa_id, target_id, "SCALES", {}, [prov]))

    def _collect_nodes(self, nodes: list, edges: list, v1, cluster: str):
        try:
            node_list = v1.list_node()
        except Exception as exc:
            logger.warning(f"Failed to list nodes: {exc}")
            return

        for node in node_list.items:
            meta = node.metadata
            status = node.status
            prov = self._prov(f"{cluster}/node/{meta.name}", str(meta.name))

            capacity = {}
            if status.capacity:
                capacity = dict(status.capacity)

            node_id = make_node_id("Infrastructure", f"k8s-node/{meta.name}", self.namespace)
            nodes.append(KGNode(
                id=node_id, type="Infrastructure", name=meta.name,
                canonical_name=f"k8s-node/{meta.name}", namespace=self.namespace,
                attributes={
                    "cluster": cluster,
                    "kind": "Node",
                    "capacity": capacity,
                    "labels": dict(meta.labels or {}),
                    "conditions": {c.type: c.status for c in (status.conditions or [])},
                },
                provenance=[prov],
            ))

    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------

    def _prov(self, source_id: str, evidence: Any) -> Provenance:
        return Provenance(
            source_type=SourceType.DEPLOYMENT,
            source_id=source_id,
            evidence=str(evidence),
            confidence=1.0,
            detail={"collector": self.name, "cluster": self.dep.cluster_name},
        )
