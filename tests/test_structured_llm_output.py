"""Structured-output contracts for LLM calls (``complete_structured``).

These pin the guarantee the document collector now relies on: passing a Pydantic
schema means the caller always gets every field back, whatever the model emitted --
missing keys are defaulted, ``"null"``-ish strings become ``None``, bare strings where
an object was asked are upgraded, and only truly unusable output re-asks (and then
fails loudly).
"""
from __future__ import annotations

import re
from typing import Optional

import pytest
from pydantic import BaseModel, Field

from kg_builder.collectors.base import CollectorResult
from kg_builder.collectors.document.doc_collector import DocumentCollector
from kg_builder.collectors.document.schemas import (
    BatchConfigClassification,
    BatchTextExtraction,
    ConfigClassification,
    ServiceMention,
    TextExtraction,
    pick_by_path,
)
from kg_builder.config import LLMSettings, settings_for
from kg_builder.enrichment.llm_client import (
    LLMError,
    LLMClient,
    LangChainLLMClient,
    call_structured,
)
from kg_builder.graph.model import KGNode, Provenance
from kg_builder.graph.ontology import NodeType, RelationType, SourceType


class _Scripted(LLMClient):
    """Fake client: replays canned JSON and records the prompts it was sent."""

    def __init__(self, answers: list[Optional[dict]]) -> None:
        self.answers = answers
        self.systems: list[str] = []
        self.users: list[str] = []

    def complete_json(self, system: str, user: str) -> Optional[dict]:
        self.systems.append(system)
        self.users.append(user)
        return self.answers.pop(0) if self.answers else {}


# ------------------------------------------------------------------ field presence
def test_missing_fields_are_filled_by_the_schema():
    parsed = _Scripted([{"services": [{"name": "OrderService"}]}]).complete_structured(
        "sys", "user", TextExtraction
    )
    payload = parsed.to_payload()

    assert payload["summary"] == ""
    assert payload["apis"] == []
    assert payload["relationships"] == []
    # The item's own absent field is defaulted too, so consumers never KeyError.
    assert payload["services"] == [{"name": "OrderService", "responsibility": ""}]


def test_json_schema_is_part_of_the_fallback_prompt():
    fake = _Scripted([{}])
    fake.complete_structured("你是资深软件架构分析师。", "user", ConfigClassification)

    assert '"category"' in fake.systems[0]
    assert "你是资深软件架构分析师。" in fake.systems[0]


def test_call_structured_wraps_a_plain_json_speaking_object():
    """A client only has to speak JSON; the schema contract is added around it."""

    class _Plain:
        def __init__(self) -> None:
            self.systems: list[str] = []

        def complete_json(self, system, user):
            self.systems.append(system)
            return {"services": ["OrderService"]}

    plain = _Plain()
    payload = call_structured(plain, "sys", "user", TextExtraction).to_payload()

    assert payload["services"] == [{"name": "OrderService", "responsibility": ""}]
    assert '"services"' in plain.systems[0]


# ---------------------------------------------------------------------- normalisation
def test_junk_values_are_normalised_not_rejected():
    fake = _Scripted([
        {
            # Strings where objects were asked, "null" instead of JSON null,
            # an out-of-enum category and a lower-cased method.
            "category": "Kubernetes",
            "owner_service": "null",
            "summary": None,
        }
    ])
    payload = fake.complete_structured("sys", "user", ConfigClassification).to_payload()

    assert payload == {"category": "other", "owner_service": None, "summary": ""}

    docs = _Scripted([{"documents": {"a.md": {"apis": ["/api/orders"], "services": "OrderService"}}}])
    out = docs.complete_structured("sys", "user", BatchTextExtraction).documents

    assert out["a.md"].apis[0].path == "/api/orders"
    assert out["a.md"].services == []


def test_relationship_aliases_survive_the_round_trip():
    parsed = TextExtraction.model_validate(
        {"relationships": [{"from": "OrderService", "to": "PayService", "type": "depends_on"}]}
    )
    # ``from``/``to`` are what the graph-emitting code reads, so the payload must
    # come back with those keys even though the fields are named differently.
    assert parsed.to_payload()["relationships"][0] == {
        "from": "OrderService",
        "to": "PayService",
        "type": "DEPENDS_ON",
        "description": "",
    }


def test_unusable_batch_entry_does_not_poison_its_siblings():
    parsed = _Scripted([{"files": {"good.yaml": {"category": "k8s"}, "bad.yaml": "nope"}}]).complete_structured(
        "sys", "user", BatchConfigClassification
    )
    assert parsed.files["good.yaml"].category == "k8s"
    assert parsed.files["bad.yaml"].category == "other"


def test_path_lookup_tolerates_spelling_drift():
    found = {"./src/a.py": "hit"}

    assert pick_by_path(found, "src/a.py") == "hit"
    assert pick_by_path(found, "docs/b.md") is None


# --------------------------------------------------------------------- re-ask / error
class _Strict(BaseModel):
    count: int
    label: str = Field(default="", alias="name")


def test_reasks_once_with_the_validation_errors():
    fake = _Scripted([{"count": "abc"}, {"count": 3}])
    assert fake.complete_structured("sys", "original user", _Strict).count == 3

    assert len(fake.users) == 2
    assert fake.users[0] == "original user"
    assert "original user" in fake.users[1] and "count" in fake.users[1]


def test_persistent_schema_violation_raises_llmerror():
    fake = _Scripted([{"count": "abc"}, {"count": "abc"}])
    with pytest.raises(LLMError, match="_Strict"):
        fake.complete_structured("sys", "user", _Strict)
    assert len(fake.users) == 2


def test_transport_error_is_not_retried_by_the_schema_layer():
    class _Down(LLMClient):
        def complete_json(self, system: str, user: str) -> Optional[dict]:
            raise LLMError("LLM request failed: connection refused")

    with pytest.raises(LLMError, match="connection refused"):
        _Down().complete_structured("sys", "user", TextExtraction)


# --------------------------------------------------------- collector integration
def _settings(tmp_path):
    return settings_for(
        tmp_path, tmp_path / "kg.json", code_analysis__enabled=False, history__enabled=False,
    )


def test_collector_uses_schemas_and_keeps_graph_facts(tmp_path, monkeypatch):
    fake = _Scripted([
        {"documents": {"a.md": {"relationships": [{"from": "OrderService", "to": "PayService"}]}}},
        {"services": []},
    ])
    monkeypatch.setattr("kg_builder.enrichment.llm_client.create_llm_client", lambda s: fake)

    collector = DocumentCollector(_settings(tmp_path))
    out = collector._llm_analyze_texts([("a.md", "OrderService calls PayService.")])

    rels = out["a.md"]["relationships"]
    assert rels[0]["from"] == "OrderService" and rels[0]["to"] == "PayService"
    assert rels[0]["type"] == "CALLS"

    # The payload still drives edge emission, with both endpoints materialised.
    result = CollectorResult(collector="document")
    doc_node = KGNode(
        id="document:a.md", type=NodeType.DOCUMENT.value, name="a.md",
        canonical_name="a.md", provenance=[Provenance(SourceType.DOCUMENT, "a.md:1", "doc")],
    )
    result.add_node(doc_node)
    collector._apply_llm_text_result(result, doc_node, "a.md", out["a.md"])

    node_ids = {n.id for n in result.nodes}
    assert {e.source for e in result.edges} <= node_ids
    assert {e.target for e in result.edges} <= node_ids
    assert any(e.relation == RelationType.CALLS.value for e in result.edges)


def test_collector_falls_back_to_heuristics_when_output_is_unusable(tmp_path, monkeypatch):
    class _Junk(LLMClient):
        def complete_json(self, system: str, user: str) -> Optional[dict]:
            return None  # nothing parseable at all

    monkeypatch.setattr("kg_builder.enrichment.llm_client.create_llm_client", lambda s: _Junk())
    collector = DocumentCollector(_settings(tmp_path))

    assert collector._llm_analyze_text("a.md", "OrderService handles orders") is None

    result = CollectorResult(collector="document")
    collector._handle_text(result, tmp_path / "a.md", "a.md", "OrderService calls /api/orders")
    assert any("heuristic" in w for w in result.warnings)
    assert [n for n in result.nodes if n.type == NodeType.SERVICE.value]


def test_collect_survives_dirty_model_output(tmp_path, monkeypatch):
    """A whole build with junk JSON: degrade to graph facts, never crash."""
    (tmp_path / "architecture.md").write_text(
        "# Arch\n\nOrderService calls PaymentService at /api/orders.\n"
    )
    (tmp_path / "order.yaml").write_text("retry: 3\n")

    class _Messy:
        def complete_json(self, system, user):
            rels = re.findall(r"=== FILE: (.+?) ===\n", user)
            if "多篇技术文档" in system:
                # Bare string for a service, lower-cased verb, and a "./" path that
                # is not the key we sent.
                return {"documents": {("./" + (rels[0] if rels else "x")): {
                    "services": ["OrderService"],
                    "apis": [{"path": "/api/orders", "method": "get"}],
                }}}
            if "多个配置文件" in system:
                return {"files": {r: {"category": "Kubernetes", "owner_service": "null"}
                                  for r in rels}}
            return {"services": [{"service_name": "OrderService",
                                  "k8s_deployment": "order-service",
                                  "source_files": "src/order.py"}]}

    monkeypatch.setattr("kg_builder.enrichment.llm_client.create_llm_client", lambda s: _Messy())
    result = DocumentCollector(_settings(tmp_path)).collect()

    names = {n.name for n in result.nodes}
    assert "null" not in names and "" not in names
    ids = {n.id for n in result.nodes}
    assert not [e for e in result.edges if e.source not in ids or e.target not in ids]
    assert "OrderService" in names
    assert result.metrics["documents_scanned"] == 2
    # The verb was normalised and the omitted summary defaulted, so the document
    # still produced facts instead of being discarded.
    assert any(n.type == NodeType.API.value and n.attributes.get("method") == "GET"
               for n in result.nodes)
    assert any(e.relation == RelationType.DEPLOYED_AS.value for e in result.edges)


# ----------------------------------------------------- native structured output
def _client() -> LangChainLLMClient:
    return LangChainLLMClient(LLMSettings(model="m", api_key="k"))


class _StubChat:
    """Stands in for ``ChatOpenAI``: hands out a scripted structured chain."""

    def __init__(self, chain=None, error: Optional[Exception] = None) -> None:
        self.chain = chain
        self.error = error
        self.built: list[str] = []

    def with_structured_output(self, schema):
        self.built.append(schema.__name__)
        if self.error is not None:
            raise self.error
        return self.chain


class _Chain:
    def __init__(self, answer) -> None:
        self.answer = answer
        self.invoked = 0

    def invoke(self, messages):
        self.invoked += 1
        return self.answer


def test_native_structured_output_is_preferred_and_reused(monkeypatch):
    client, chain = _client(), _Chain(TextExtraction(services=[ServiceMention(name="OrderService")]))
    stub = _StubChat(chain=chain)
    client._chat = stub
    monkeypatch.setattr(
        client, "complete_json",
        lambda system, user: pytest.fail("JSON fallback should not be used"),
    )

    parsed = client.complete_structured("sys", "user", TextExtraction)

    assert parsed.services[0].name == "OrderService"
    assert chain.invoked == 1
    # The chain is built once per schema and reused, not rebuilt per document batch.
    client.complete_structured("sys", "user", TextExtraction)
    assert chain.invoked == 2 and stub.built == ["TextExtraction"]


def test_endpoint_without_structured_output_falls_back_to_json(monkeypatch):
    client = _client()
    client._chat = _StubChat(error=TypeError("this model does not support tools"))
    asked: list[str] = []
    monkeypatch.setattr(
        client, "complete_json",
        lambda system, user: asked.append(system) or {"services": [{"name": "OrderService"}]},
    )

    parsed = client.complete_structured("sys", "user", TextExtraction)

    assert parsed.services[0].name == "OrderService"
    # Probed once, then switched off for the rest of the run.
    assert client._native_structured is False
    assert len(asked) == 1 and '"services"' in asked[0]
    client.complete_structured("sys", "user", TextExtraction)
    assert len(asked) == 2 and client._chat.built == ["TextExtraction"]


def test_native_answer_that_is_raw_json_dict_still_validates(monkeypatch):
    client = _client()
    client._chat = _StubChat(chain=_Chain({"services": [{"name": "OrderService"}]}))
    monkeypatch.setattr(
        client, "complete_json",
        lambda system, user: pytest.fail("JSON fallback should not be used"),
    )

    assert client.complete_structured("sys", "user", TextExtraction).services[0].name == "OrderService"
