"""K8s topology tests: Pod -> workload and Service -> Pod edges.

The collector talks to the Kubernetes API, so these tests drive the mapping
methods with lightweight stand-ins that expose the same attribute shape.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from kg_builder.collectors.deployment.k8s import K8sCollector
from kg_builder.config import settings_for


def _deployment(name: str, pod_labels: dict):
    return NS(
        metadata=NS(name=name, labels={"app": name}),
        spec=NS(
            replicas=2,
            strategy=NS(type="RollingUpdate"),
            template=NS(metadata=NS(labels=pod_labels)),
        ),
        status=NS(ready_replicas=2),
    )


def _service(name: str, selector: dict):
    return NS(
        metadata=NS(name=name, labels={}),
        spec=NS(
            type="ClusterIP",
            cluster_ip="10.0.0.1",
            ports=[NS(port=80, target_port=8080, protocol="TCP")],
            selector=selector,
        ),
    )


def _pod(name: str, labels: dict):
    return NS(
        metadata=NS(name=name, labels=labels),
        spec=NS(
            containers=[NS(resources=None, readiness_probe=NS(), liveness_probe=None)],
            node_name="node-1",
        ),
        status=NS(
            phase="Running",
            container_statuses=[NS(restart_count=0)],
            qos_class="Burstable",
        ),
    )


def test_pod_links_to_workload_and_service(tmp_path):
    settings = settings_for(tmp_path, tmp_path / "kg.json")
    k8s = K8sCollector(settings.deployment, "")
    k8s._workloads = []
    k8s._service_selectors = []

    apps = NS(list_namespaced_deployment=lambda ns: NS(items=[
        _deployment("order-service", {"app": "order-service"}),
    ]))
    core = NS(
        list_namespaced_service=lambda ns: NS(items=[
            _service("order-service", {"app": "order-service"}),
        ]),
        list_namespaced_pod=lambda ns: NS(items=[
            _pod("order-service-abc", {"app": "order-service", "tier": "backend"}),
            _pod("unrelated-pod", {"app": "other"}),
        ]),
    )

    nodes, edges = [], []
    k8s._collect_deployments(nodes, edges, apps, "prod", "cluster")
    k8s._collect_services(nodes, edges, core, "prod", "cluster")
    k8s._collect_pods(nodes, edges, core, "prod", "cluster")

    node_ids = {n.id for n in nodes}
    instance_of = [e for e in edges if e.attributes.get("kind") == "instance_of"]
    routes_to = [e for e in edges if e.attributes.get("kind") == "routes_to"]

    assert len(instance_of) == 1, "only the matching pod belongs to the deployment"
    assert len(routes_to) == 1, "only the matching pod is selected by the service"
    for edge in instance_of + routes_to:
        assert edge.source in node_ids and edge.target in node_ids

    pod_names = {n.name for n in nodes if n.type == "Pod"}
    assert pod_names == {"order-service-abc", "unrelated-pod"}
