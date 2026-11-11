"""Tests for cross-source entity resolution and document relation semantics.

Two defects are covered here:

* the same service discovered as ``OrderService`` (code) and ``order-service``
  (Kubernetes / docs) stayed two nodes, so ``Service --DEPLOYED_AS--> Deployment``
  looked like a self-reference;
* prose documents were graphed as ``Document --DEPENDS_ON--> Service``, which
  asserts a dependency that does not exist.
"""
from __future__ import annotations

from kg_builder.collectors.base import CollectorResult
from kg_builder.collectors.document import DocumentCollector
from kg_builder.config import settings_for
from kg_builder.graph.entity_resolution import api_key, resolve_entities, service_key
from kg_builder.graph.knowledge_graph import KnowledgeGraph
from kg_builder.graph.model import KGEdge, KGNode, Provenance, make_node_id
from kg_builder.graph.ontology import NodeType, RelationType, SourceType
from kg_builder.pipeline import BuildPipeline


def _service(name: str, source_type: SourceType, source_id: str) -> KGNode:
    return KGNode(
        id=make_node_id(NodeType.SERVICE.value, name, ""),
        type=NodeType.SERVICE.value,
        name=name,
        canonical_name=name,
        provenance=[Provenance(source_type, source_id, f"service {name}")],
    )


def _deployment(name: str) -> KGNode:
    canon = f"prod/{name}"
    return KGNode(
        id=make_node_id(NodeType.DEPLOYMENT.value, canon, ""),
        type=NodeType.DEPLOYMENT.value,
        name=name,
        canonical_name=canon,
        provenance=[Provenance(SourceType.DEPLOYMENT, f"cluster/prod/deployment/{name}", name)],
    )


def _api(label: str, source_type: SourceType, source_id: str) -> KGNode:
    return KGNode(
        id=make_node_id(NodeType.API.value, label.lower(), ""),
        type=NodeType.API.value,
        name=label,
        canonical_name=label.lower(),
        provenance=[Provenance(source_type, source_id, f"api {label}")],
    )


def test_service_key_folds_case_and_separators():
    assert service_key("OrderService") == service_key("order-service") == service_key("order_service")
    assert service_key("OrderService") != service_key("InventoryService")
    assert service_key("") == ""


def test_resolve_merges_variants_and_rewires_edges():
    graph = KnowledgeGraph()
    code_svc = _service("OrderService", SourceType.SOURCE_CODE, "src/order_service.py:5")
    doc_svc = _service("order-service", SourceType.DOCUMENT, "docs/architecture.md:3")
    deploy = _deployment("order-service")
    for node in (code_svc, doc_svc, deploy):
        graph.add_node(node)
    graph.add_edge(KGEdge(
        code_svc.id, deploy.id, RelationType.DEPLOYED_AS.value,
        provenance=[Provenance(SourceType.SOURCE_CODE, "src/order_service.py:9", "deployed")],
    ))
    graph.add_edge(KGEdge(
        doc_svc.id, deploy.id, RelationType.DEPLOYED_AS.value,
        provenance=[Provenance(SourceType.DOCUMENT, "docs/architecture.md:7", "deployed")],
    ))

    summary = resolve_entities(graph)

    assert summary["merged_nodes"] == 1
    services = graph.find_nodes(type=NodeType.SERVICE)
    assert len(services) == 1
    primary = services[0]
    # Strongest evidence keeps the id and the display name.
    assert primary.name == "OrderService"
    assert primary.attributes["aliases"] == ["order-service"]
    assert len(primary.provenance) == 2
    # The two DEPLOYED_AS facts are now one edge carrying both sources.
    deployed = [e for e in graph.edges if e.relation == RelationType.DEPLOYED_AS.value]
    assert len(deployed) == 1
    assert deployed[0].source == primary.id
    assert len(deployed[0].provenance) == 2


def test_resolve_keeps_distinct_services():
    graph = KnowledgeGraph()
    graph.add_node(_service("OrderService", SourceType.SOURCE_CODE, "a.py:1"))
    graph.add_node(_service("InventoryService", SourceType.SOURCE_CODE, "b.py:1"))

    summary = resolve_entities(graph)

    assert summary["merged_nodes"] == 0
    assert len(graph.find_nodes(type=NodeType.SERVICE)) == 2


def test_api_key_folds_template_and_case_but_keeps_verb():
    # Verb case, ``{id}``/``:id`` parameter form and trailing slash all fold.
    assert api_key("GET /order/{order_id}") == api_key("get /order/:id") == api_key("GET /order/{id}/")
    # ...but a different verb is a distinct REST operation, not a variant.
    assert api_key("POST /order/{id}") != api_key("GET /order/{id}")
    # Different paths never collide.
    assert api_key("GET /order/{id}") != api_key("GET /payment/{id}")
    assert api_key("") == ""


def test_resolve_merges_api_spelling_variants():
    """A doc that writes ``get /order/:id`` folds into the code's ``GET /order/{order_id}``."""
    graph = KnowledgeGraph()
    code_api = _api("GET /order/{order_id}", SourceType.SOURCE_CODE, "services/order.py:58")
    doc_api = _api("get /order/:id", SourceType.DOCUMENT, "docs/architecture.md:20")
    svc = _service("OrderService", SourceType.SOURCE_CODE, "services/order.py:20")
    doc = KGNode(id=make_node_id(NodeType.DOCUMENT.value, "architecture.md", ""),
                 type=NodeType.DOCUMENT.value, name="architecture.md",
                 canonical_name="architecture.md",
                 provenance=[Provenance(SourceType.DOCUMENT, "docs/architecture.md:1", "doc")])
    for n in (code_api, doc_api, svc, doc):
        graph.add_node(n)
    graph.add_edge(KGEdge(svc.id, code_api.id, RelationType.EXPOSES.value))
    graph.add_edge(KGEdge(doc.id, doc_api.id, RelationType.MENTIONS.value))

    summary = resolve_entities(graph)

    assert summary["merged_nodes"] == 1
    apis = graph.find_nodes(type=NodeType.API)
    assert len(apis) == 1
    primary = apis[0]
    # Strongest source (source_code) keeps the id and human-readable display name.
    assert primary.name == "GET /order/{order_id}"
    assert primary.attributes["aliases"] == ["get /order/:id"]
    # Both cross-source edges now touch the single surviving endpoint.
    assert {e.target for e in graph.edges} == {primary.id}


def test_resolve_keeps_distinct_api_verbs():
    graph = KnowledgeGraph()
    graph.add_node(_api("GET /orders", SourceType.SOURCE_CODE, "a.py:1"))
    graph.add_node(_api("POST /orders", SourceType.SOURCE_CODE, "a.py:2"))

    summary = resolve_entities(graph)

    assert summary["merged_nodes"] == 0
    assert len(graph.find_nodes(type=NodeType.API)) == 2


def test_merge_drops_self_loops():
    graph = KnowledgeGraph()
    a = _service("PaymentService", SourceType.SOURCE_CODE, "pay.py:1")
    b = _service("payment-service", SourceType.DOCUMENT, "k8s/pay.yaml:3")
    graph.add_node(a)
    graph.add_node(b)
    graph.add_edge(KGEdge(a.id, b.id, RelationType.CALLS.value))

    resolve_entities(graph)

    # After folding, "A calls A" carries no information and must not survive.
    assert graph.edges == []


def test_document_edges_are_mentions_not_dependencies(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "architecture.md").write_text(
        "# Architecture\n\nOrderService calls PaymentService over HTTPS.\n", encoding="utf-8"
    )
    settings = settings_for(repo, tmp_path / "kg.json", enrich=False, code_analysis__enabled=False)

    result = DocumentCollector(settings).collect()

    doc_ids = {n.id for n in result.nodes if n.type == NodeType.DOCUMENT.value}
    from_docs = [e for e in result.edges if e.source in doc_ids]
    assert from_docs, "document should be linked to the entities it describes"
    assert {e.relation for e in from_docs} == {RelationType.MENTIONS.value}


def test_llm_relationships_materialise_missing_endpoints(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = settings_for(repo, tmp_path / "kg.json", enrich=False, code_analysis__enabled=False)
    collector = DocumentCollector(settings)

    doc_id = make_node_id(NodeType.DOCUMENT.value, "a.md", "")
    doc_node = KGNode(
        id=doc_id, type=NodeType.DOCUMENT.value, name="a.md", canonical_name="a.md",
        provenance=[Provenance(SourceType.DOCUMENT, "a.md:1", "doc")],
    )
    result = CollectorResult(collector="document")
    result.add_node(doc_node)

    collector._apply_llm_text_result(result, doc_node, "a.md", {
        "services": [{"name": "OrderService"}],
        "relationships": [{"from": "OrderService", "to": "GhostService", "type": "CALLS"}],
        "summary": "order flow",
    })

    node_ids = {n.id for n in result.nodes}
    calls = [e for e in result.edges if e.relation == RelationType.CALLS.value]
    assert len(calls) == 1
    # Without materialising GhostService the pipeline would drop this edge.
    assert calls[0].source in node_ids
    assert calls[0].target in node_ids


def test_manifest_service_is_linked_to_its_workload(tmp_path):
    repo = tmp_path / "repo"
    (repo / "k8s").mkdir(parents=True)
    (repo / "k8s" / "order-service.yaml").write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: order-service\n"
        "  namespace: prod\n"
        "spec:\n"
        "  replicas: 2\n"
        "  template:\n"
        "    metadata:\n"
        "      labels:\n"
        "        app: order-service\n",
        encoding="utf-8",
    )
    (repo / "k8s" / "order-svc.yaml").write_text(
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: order-service\n"
        "  namespace: prod\n"
        "spec:\n"
        "  type: ClusterIP\n"
        "  selector:\n"
        "    app: order-service\n"
        "  ports:\n"
        "    - port: 80\n",
        encoding="utf-8",
    )
    settings = settings_for(repo, tmp_path / "kg.json", enrich=False, code_analysis__enabled=False)

    result = DocumentCollector(settings).collect()

    links = [
        e for e in result.edges
        if e.relation == RelationType.DEPENDS_ON.value and e.attributes.get("kind") == "routes_to"
    ]
    assert len(links) == 1
    assert result.metrics["k8s_service_links"] == 1


def test_lock_files_are_not_flattened_into_configs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text(
        '{"lockfileVersion": 3, "packages": {"a": {"version": "1.0.0"}}}', encoding="utf-8"
    )
    (repo / "settings.yaml").write_text("retry: 3\n", encoding="utf-8")
    settings = settings_for(repo, tmp_path / "kg.json", enrich=False, code_analysis__enabled=False)

    result = DocumentCollector(settings).collect()

    document_names = [n.name for n in result.nodes if n.type == NodeType.DOCUMENT.value]
    assert "package-lock.json" not in document_names
    assert any(n.type == NodeType.CONFIG.value for n in result.nodes)


def test_pipeline_records_build_meta(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "k8s").mkdir()
    (repo / "k8s" / "order-service.yaml").write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: order-service\n"
        "  namespace: prod\n"
        "spec:\n"
        "  replicas: 2\n",
        encoding="utf-8",
    )
    settings = settings_for(
        repo, tmp_path / "kg.json",
        enrich=False, code_analysis__enabled=False, history__enabled=False,
    )

    report = BuildPipeline(settings).run()

    meta = report.graph.meta
    assert meta["build"]["enriched"] is False
    assert meta["build"]["node_count"] == report.stats["node_count"]
    assert [c["name"] for c in meta["build"]["collectors"]] == ["document"]
    assert "entity_resolution" in meta
    assert "built_at" in meta["build"]
