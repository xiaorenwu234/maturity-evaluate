"""Unit tests for the knowledge-graph core model."""
from __future__ import annotations

from kg_builder.graph import KnowledgeGraph, Provenance
from kg_builder.graph.model import KGEdge, KGNode, make_node_id
from kg_builder.graph.ontology import NodeType, RelationType, SourceType
from kg_builder.graph.serialization import dict_to_graph, graph_to_dict, to_cypher


def _prov(source=SourceType.SOURCE_CODE, sid="a.py:1", ev="x", conf=1.0):
    return Provenance(source_type=source, source_id=sid, evidence=ev, confidence=conf)


def test_node_id_is_deterministic():
    a = make_node_id(NodeType.SERVICE.value, "Order", "")
    b = make_node_id(NodeType.SERVICE.value, "Order", "")
    assert a == b


def test_add_edge_requires_endpoints():
    g = KnowledgeGraph()
    n = g.upsert_node(NodeType.SERVICE, "Order", provenance=[_prov()])
    try:
        g.add_edge(KGEdge(source=n.id, target="missing", relation=RelationType.CALLS.value))
    except KeyError:
        return
    raise AssertionError("expected KeyError for missing endpoint")


def test_provenance_merges_across_sources():
    g = KnowledgeGraph()
    svc = g.upsert_node(NodeType.SERVICE, "Payment", provenance=[_prov(SourceType.SOURCE_CODE, "payment.py:1", "class PaymentService")])
    # Same logical node discovered by deployment:
    svc2 = g.upsert_node(NodeType.SERVICE, "Payment", provenance=[_prov(SourceType.DEPLOYMENT, "cluster/prod/deployment/payment", "replica=1")])
    assert svc.id == svc2.id
    stored = g.get_node(svc.id)
    assert SourceType.SOURCE_CODE.value in stored.sources
    assert SourceType.DEPLOYMENT.value in stored.sources


def test_duplicate_identical_provenance_not_added_twice():
    g = KnowledgeGraph()
    n = g.upsert_node(NodeType.SERVICE, "Order", provenance=[_prov(ev="same")])
    n2 = _prov(ev="same")
    g.add_node(KGNode(id=n.id, type=n.type, name=n.name, provenance=[n2]))
    assert len(g.get_node(n.id).provenance) == 1


def test_serialization_roundtrip():
    g = KnowledgeGraph()
    a = g.upsert_node(NodeType.SERVICE, "Order", provenance=[_prov()])
    b = g.upsert_node(NodeType.SERVICE, "Payment", provenance=[_prov()])
    g.upsert_edge(a.id, b.id, RelationType.CALLS, {"level": "service"}, provenance=[_prov(ev="calls")])
    data = graph_to_dict(g)
    g2 = dict_to_graph(data)
    assert g2.stats()["node_count"] == 2
    assert g2.stats()["edge_count"] == 1
    edge = g2.edges[0]
    assert edge.relation == RelationType.CALLS.value
    assert edge.provenance[0].evidence == "calls"


def test_to_cypher_emits_importable_statements():
    """Regression: the Cypher export must be valid, importable MERGE/SET."""
    g = KnowledgeGraph()
    a = g.upsert_node(NodeType.SERVICE, "Order", provenance=[_prov(source=SourceType.LLM, ev='{"x": 1}')])
    b = g.upsert_node(NodeType.DATABASE, "orders db", provenance=[_prov()])
    g.upsert_edge(a.id, b.id, RelationType.READS, {"op": "select"}, provenance=[_prov()])
    text = to_cypher(g)

    # A bound node variable + ``SET prop = ...`` (the old bug emitted ``SET "literal"``).
    assert "MERGE (n:`Service`" in text
    assert "SET n.name =" in text
    assert 'SET "' not in text
    # Relationship uses a bound ``r`` var and typed endpoints.
    assert "MATCH (a:`Service`" in text and "(b:`Database`" in text
    assert "MERGE (a)-[r:`READS`]->(b)" in text
    # JSON-in-string values must have inner quotes escaped (no broken lists/objects).
    assert '\\"' in text
    # sources exported as a Neo4j string list, provenance as a JSON string.
    assert "n.sources = [" in text and "n.provenance = " in text

