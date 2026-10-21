"""End-to-end tests for the build pipeline over the sample repo.

Semantic enrichment is LLM-only, so tests inject a fake :class:`LLMClient`
(dependency injection for tests -- NOT a production fallback path).
"""
from __future__ import annotations

from pathlib import Path

from kg_builder.config import settings_for
from kg_builder.collectors.deployment import DeploymentCollector
from kg_builder.enrichment.llm_client import LLMClient
from kg_builder.graph.ontology import NodeType, RelationType, SourceType
from kg_builder.graph.serialization import load_graph
from kg_builder.pipeline import BuildPipeline

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_repo"


class FakeLLM(LLMClient):
    """A deterministic stand-in for the model, keyed by entity name."""

    def __init__(self, answers: dict) -> None:
        self._answers = answers
        self.calls = 0

    def complete_json(self, system: str, user: str) -> dict:
        self.calls += 1
        return self._answers


def _service_answers() -> dict:
    return {
        "OrderService": {"business_domain": "order", "is_core_business": True,
                         "responsibility": "\u4e0b\u5355\u4e0e\u8ba2\u5355\u7f16\u6392"},
        "PaymentService": {"business_domain": "payment", "is_core_business": True,
                           "responsibility": "\u652f\u4ed8"},
        "InventoryService": {"business_domain": "inventory", "is_core_business": False,
                             "responsibility": "\u5e93\u5b58"},
    }


def test_pipeline_build_structural(tmp_path):
    out = tmp_path / "kg.json"
    settings = settings_for(SAMPLE, out, enrich=False)
    report = BuildPipeline(settings).run()

    # Core entities from code should be present (no LLM needed for structure).
    assert report.stats["nodes_by_type"].get(NodeType.SERVICE.value, 0) >= 3
    assert report.stats["relations_by_type"].get(RelationType.CALLS.value, 0) >= 1
    assert report.enrichment == {"skipped": True}
    # Persisted + reloadable.
    assert out.exists()
    reloaded = load_graph(out)
    assert reloaded.stats()["node_count"] == report.stats["node_count"]


def test_semantic_enrichment_via_llm(tmp_path):
    settings = settings_for(SAMPLE, tmp_path / "kg.json")
    fake = FakeLLM(_service_answers())
    report = BuildPipeline(settings).run(write_outputs=False, llm_client=fake)

    order = report.graph.find_nodes(type=NodeType.SERVICE, name="OrderService")[0]
    semantic = order.attributes.get("semantic", {})
    assert semantic.get("is_core_business") is True
    assert semantic.get("business_domain") == "order"
    # Enrichment provenance is recorded as model-inferred (source=llm).
    assert any(p.source_type == SourceType.LLM.value for p in order.provenance)
    assert fake.calls >= 1


def test_enrichment_requires_configured_llm(tmp_path):
    """With enrichment on and no model configured, the build fails loudly."""
    import pytest

    from kg_builder.enrichment.llm_client import create_llm_client

    settings = settings_for(SAMPLE, tmp_path / "kg.json")  # llm.model/api_key empty
    with pytest.raises(Exception):
        create_llm_client(settings.llm)


def test_cross_source_merge_code_and_deployment(tmp_path):
    """Deployment Payment facts merge onto the code-derived PaymentService node."""
    from kg_builder.collectors import build_collectors

    settings = settings_for(SAMPLE, tmp_path / "kg.json", enrich=False)
    pipeline = BuildPipeline(settings)
    collectors = build_collectors(settings)

    # Simulate what the future K8s client would return, mapped by the deployment
    # collector into a CollectorResult (source=deployment provenance).
    dep = DeploymentCollector(settings)
    wl = {"service": "PaymentService", "name": "payment", "namespace": "prod",
          "cluster": "prod-cluster", "replicas": 1, "readiness_probe": False}
    dep_res = dep.collect()
    dep._map_workload(dep_res, wl)

    class _FixtureCollector:
        name = "deployment-fixture"

        def is_enabled(self):
            return True

        def collect(self):
            return dep_res

    report = pipeline.run(collectors=collectors + [_FixtureCollector()], write_outputs=False)
    payment = report.graph.find_nodes(type=NodeType.SERVICE, name="PaymentService")[0]
    assert SourceType.SOURCE_CODE.value in payment.sources
    assert SourceType.DEPLOYMENT.value in payment.sources

    deployed = report.graph.out_edges(payment.id, RelationType.DEPLOYED_AS)
    assert deployed
