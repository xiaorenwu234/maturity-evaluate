"""Kubernetes manifest parser.

Handles YAML files containing K8s resource definitions (Deployment, Service,
StatefulSet, DaemonSet, HPA, Ingress, ConfigMap, Secret, etc.).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import yaml

from ....graph.model import KGEdge, KGNode, Provenance, make_node_id
from ....graph.ontology import NodeType, RelationType, SourceType
from .base import BaseConfigParser, ParseResult

logger = logging.getLogger(__name__)

_K8S_KINDS = {
    "Deployment", "Service", "StatefulSet", "DaemonSet", "ReplicaSet",
    "Pod", "HPA", "HorizontalPodAutoscaler", "Ingress", "ConfigMap",
    "Secret", "Namespace", "Job", "CronJob", "NetworkPolicy",
    "PersistentVolumeClaim", "ServiceAccount", "Role", "RoleBinding",
    "ClusterRole", "ClusterRoleBinding",
}


class K8sParser(BaseConfigParser):
    category = "k8s"

    @staticmethod
    def can_parse(rel_path: str, ext: str, content: str) -> bool:
        if ext not in (".yaml", ".yml"):
            return False
        # Quick header check for apiVersion + kind
        head = content[:500]
        return "apiVersion:" in head and "kind:" in head

    def parse(self, rel_path: str, content: str, owner_service: Optional[str],
              namespace: str) -> ParseResult:
        result = ParseResult()
        try:
            docs = list(yaml.safe_load_all(content))
        except Exception as exc:
            result.warnings.append(f"k8s parse failed: {rel_path}: {exc}")
            return result

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind", "")
            meta = doc.get("metadata", {}) or {}
            name = meta.get("name", "")
            ns = meta.get("namespace", "")
            labels = meta.get("labels", {}) or {}

            if kind not in _K8S_KINDS or not name:
                continue

            prov = Provenance(
                source_type=SourceType.DOCUMENT,
                source_id=f"doc_parser:k8s:{rel_path}",
                evidence=f"K8s {kind}/{name} from static manifest",
                confidence=0.9,
                detail={"parser": "k8s", "file": rel_path},
            )

            if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"):
                spec = doc.get("spec", {}) or {}
                node_id = make_node_id(NodeType.DEPLOYMENT.value, f"{ns}/{name}" if ns else name, namespace)
                result.nodes.append(KGNode(
                    id=node_id, type=NodeType.DEPLOYMENT.value, name=name,
                    canonical_name=f"{ns}/{name}" if ns else name, namespace=namespace,
                    attributes={
                        "kind": kind, "k8s_namespace": ns,
                        "replicas": spec.get("replicas"),
                        "labels": labels, "source_file": rel_path,
                    },
                    provenance=[prov],
                ))
                # Link to owner service if known
                if owner_service:
                    svc_id = make_node_id(NodeType.SERVICE.value, owner_service, namespace)
                    result.edges.append(KGEdge(svc_id, node_id, RelationType.DEPLOYED_AS.value,
                                               {"source": "doc_k8s_parser"}, [prov]))

            elif kind == "Service":
                svc_spec = doc.get("spec", {}) or {}
                ports = svc_spec.get("ports", [])
                node_id = make_node_id(NodeType.SERVICE.value, name, namespace)
                result.nodes.append(KGNode(
                    id=node_id, type=NodeType.SERVICE.value, name=name,
                    canonical_name=name, namespace=namespace,
                    attributes={
                        "service_type": svc_spec.get("type"),
                        "ports": ports, "labels": labels, "source_file": rel_path,
                    },
                    provenance=[prov],
                ))

            elif kind in ("HorizontalPodAutoscaler", "HPA"):
                spec = doc.get("spec", {}) or {}
                node_id = make_node_id("AutoScaling", f"{ns}/{name}" if ns else name, namespace)
                result.nodes.append(KGNode(
                    id=node_id, type="AutoScaling", name=name,
                    canonical_name=f"{ns}/{name}" if ns else name, namespace=namespace,
                    attributes={
                        "min_replicas": spec.get("minReplicas"),
                        "max_replicas": spec.get("maxReplicas"),
                        "source_file": rel_path,
                    },
                    provenance=[prov],
                ))

            elif kind == "Ingress":
                node_id = make_node_id(NodeType.API.value, f"{ns}/{name}" if ns else name, namespace)
                rules = doc.get("spec", {}).get("rules", []) if doc.get("spec") else []
                result.nodes.append(KGNode(
                    id=node_id, type=NodeType.API.value, name=name,
                    canonical_name=f"{ns}/{name}" if ns else name, namespace=namespace,
                    attributes={"rules_count": len(rules), "labels": labels, "source_file": rel_path},
                    provenance=[prov],
                ))

            elif kind == "ConfigMap":
                data = doc.get("data", {}) or {}
                for key, value in data.items():
                    cfg_id = make_node_id(NodeType.CONFIG.value, key.lower(), namespace)
                    result.nodes.append(KGNode(
                        id=cfg_id, type=NodeType.CONFIG.value, name=key,
                        canonical_name=key.lower(), namespace=namespace,
                        attributes={"value": str(value)[:120], "source_file": rel_path, "from_configmap": name},
                        provenance=[prov],
                    ))

            # Other kinds (Secret, Job, CronJob, etc.) get generic Infrastructure nodes
            else:
                node_id = make_node_id("Infrastructure", f"k8s-{kind}/{name}", namespace)
                result.nodes.append(KGNode(
                    id=node_id, type="Infrastructure", name=name,
                    canonical_name=f"k8s-{kind}/{name}", namespace=namespace,
                    attributes={"kind": kind, "k8s_namespace": ns, "labels": labels, "source_file": rel_path},
                    provenance=[prov],
                ))

        return result
