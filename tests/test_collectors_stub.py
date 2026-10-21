"""Tests for the (stub) deployment + runtime collectors' mapping logic.

The cloud/cluster *fetching* is intentionally unimplemented, but the mapping from
a normalised observation record to graph primitives (with ``source=deployment`` /
``source=runtime`` provenance) is tested here so the evidence-chain behaviour is
already correct for when the real clients are wired in.
"""
from __future__ import annotations

from pathlib import Path

from kg_builder.collectors.deployment import DeploymentCollector
from kg_builder.collectors.runtime import RuntimeCollector
from kg_builder.config import settings_for
from kg_builder.graph.ontology import NodeType, RelationType, SourceType

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_repo"


def test_deployment_stub_collect_is_empty_and_warns():
    settings = settings_for(SAMPLE)  # deployment disabled by default
    settings.deployment.enabled = True  # force-enable to exercise collect()
    collector = DeploymentCollector(settings)
    result = collector.collect()
    # No live cluster -> empty nodes, explicit stub warning.
    assert result.nodes == []
    assert any("stub" in w for w in result.warnings)


def test_deployment_mapping_produces_deployed_as_and_provenance():
    settings = settings_for(SAMPLE)
    collector = DeploymentCollector(settings)
    wl = {
        "service": "Payment",
        "name": "payment",
        "namespace": "prod",
        "cluster": "prod-cluster",
        "replicas": 1,
        "readiness_probe": False,
        "liveness_probe": False,
        "region": "cn-north-1a",
    }
    res = collector.collect()  # empty (stub fetch)
    collector._map_workload(res, wl)
    deps = [n for n in res.nodes if n.type == NodeType.DEPLOYMENT.value]
    assert deps
    assert deps[0].attributes["replicas"] == 1
    deployed_as = [e for e in res.edges if e.relation == RelationType.DEPLOYED_AS.value]
    assert deployed_as
    prov = deployed_as[0].provenance[0]
    assert prov.source_type == SourceType.DEPLOYMENT.value
    assert prov.confidence == 1.0


def test_runtime_mapping_produces_observed_by():
    settings = settings_for(SAMPLE)
    collector = RuntimeCollector(settings)
    res = collector.collect()  # empty (stub fetch)
    collector._map_metric(res, {"service": "Order", "name": "p99_latency_ms", "value": 812})
    metrics = [n for n in res.nodes if n.type == NodeType.METRIC.value]
    assert metrics
    assert any(e.relation == RelationType.OBSERVED_BY.value for e in res.edges)
