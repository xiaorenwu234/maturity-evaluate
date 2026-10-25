"""Tests for the deployment + runtime collectors.

Deployment collector connects to a real K8s cluster, so these tests verify:
1. Disabled collector returns empty result
2. Enabled but no kubeconfig produces a warning
3. Provenance is correctly tagged with source=deployment
"""
from __future__ import annotations

from pathlib import Path

from kg_builder.collectors.deployment import DeploymentCollector
from kg_builder.collectors.runtime import RuntimeCollector
from kg_builder.config import settings_for
from kg_builder.graph.ontology import NodeType, RelationType, SourceType

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_repo"


def test_deployment_disabled_returns_empty():
    """When deployment.enabled is False, collect() returns empty result."""
    settings = settings_for(SAMPLE)  # deployment disabled by default
    collector = DeploymentCollector(settings)
    result = collector.collect()
    assert result.nodes == []
    assert result.edges == []


def test_deployment_enabled_no_kubeconfig_warns():
    """When enabled but k8s SDK or kubeconfig is unavailable, warns."""
    settings = settings_for(SAMPLE)
    settings.deployment.enabled = True
    collector = DeploymentCollector(settings)
    result = collector.collect()
    # Should have at least one warning (either k8s not installed or kubeconfig load failed)
    assert len(result.warnings) >= 1


def test_deployment_provenance_structure():
    """Verify K8s sub-collector _prov helper produces correct provenance."""
    from kg_builder.collectors.deployment.k8s import K8sCollector
    settings = settings_for(SAMPLE)
    k8s = K8sCollector(settings.deployment, settings.namespace)
    prov = k8s._prov("test-cluster/default/deployment/nginx", "nginx deployment")
    assert prov.source_type == SourceType.DEPLOYMENT.value
    assert prov.confidence == 1.0
    assert prov.source_id == "test-cluster/default/deployment/nginx"
    assert prov.detail["cluster"] == "default"


def test_runtime_mapping_produces_observed_by():
    settings = settings_for(SAMPLE)
    collector = RuntimeCollector(settings)
    res = collector.collect()  # empty (stub fetch)
    collector._map_metric(res, {"service": "Order", "name": "p99_latency_ms", "value": 812})
    metrics = [n for n in res.nodes if n.type == NodeType.METRIC.value]
    assert metrics
    assert any(e.relation == RelationType.OBSERVED_BY.value for e in res.edges)
