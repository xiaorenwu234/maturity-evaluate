"""Regression tests for the consistency fixes (findings A1-A4 / B5-B8).

Each test pins one contract that was previously violated:

* A1 document LLM mappings must materialise Deployment / Module endpoints.
* A2 ``SCALES`` is a first-class relation type.
* A3 Java class/method qnames must match Joern's, and path spellings bridge.
* A4 code-derived Config nodes expose ``value`` / ``source_file``.
* B5 CFGs live only in the ``.cfg.json`` side-car, not on Function nodes.
* B6 provenance language must reflect the artifact, not always "python".
* B8 document LLM inference is recorded as ``source=llm``.
"""

from __future__ import annotations

from pathlib import Path

from kg_builder.collectors.base import CollectorResult, module_name_of
from kg_builder.collectors.code.collector import CodeCollector
from kg_builder.collectors.code.java.cpg_parser import (
    JavaCallIndex,
    JavaCallRecord,
    JavaMethodRecord,
    resolve_java_call,
)
from kg_builder.collectors.code.java.java_ir_adapter import _class_qname
from kg_builder.collectors.code.schema.artifact import (
    ConfigRef,
    FileArtifact,
    FunctionDef,
)
from kg_builder.collectors.document import DocumentCollector
from kg_builder.config import settings_for
from kg_builder.graph.ontology import NodeType, RelationType, SourceType


def _settings(tmp_path):
    return settings_for(
        tmp_path, tmp_path / "kg.json", enrich=False, code_analysis__enabled=False,
    )


# --------------------------------------------------------------------------- A2
def test_scales_is_a_registered_relation():
    assert RelationType.is_valid("SCALES")
    assert RelationType.SCALES.value == "SCALES"


# --------------------------------------------------------------------------- A3
def test_java_class_qname_does_not_duplicate_file_segment():
    # FileArtifact.module already ends with the file name.
    assert _class_qname("com.example.OrderService", "OrderService") == (
        "com.example.OrderService"
    )
    # A second class in the same file still gets its own segment.
    assert _class_qname("com.example.OrderService", "Helper") == (
        "com.example.OrderService.Helper"
    )


def test_java_call_index_bridges_path_spellings():
    repo_file = "src/main/java/com/example/OrderService.java"
    index = JavaCallIndex()
    index.methods_by_file[repo_file] = [
        JavaMethodRecord(
            full_name="com.example.OrderService.createOrder",
            name="createOrder",
            class_name="OrderService",
            file=repo_file,
            line=10,
            end_line=25,
        )
    ]
    index.calls_by_pos[(repo_file, 20)] = [
        JavaCallRecord(
            caller_full_name="com.example.OrderService.createOrder",
            callee_full_name="com.example.PaymentService.pay",
            file=repo_file,
            line=20,
        )
    ]

    # The collector looks calls up with the repo-relative path while Joern may
    # report a bare file name; both spellings must resolve.
    for spelling in (repo_file, "OrderService.java", f"/work/repo/{repo_file}"):
        resolved = resolve_java_call(index, spelling, 20, "pay")
        assert resolved is not None
        assert resolved.callee_qname == "com.example.PaymentService.pay"
        assert resolved.caller_qname == "com.example.OrderService.createOrder"


def test_module_keys_agree_across_collectors():
    assert module_name_of("services/order_service.py") == "services.order_service"
    assert module_name_of("pkg/__init__.py") == "pkg"
    assert module_name_of("src/main/java/com/example/OrderService.java") == (
        "com.example.OrderService"
    )


# ----------------------------------------------------------------------- A4 / B5
def test_code_emit_config_keys_and_no_inline_cfg(tmp_path):
    collector = CodeCollector(_settings(tmp_path))
    result = CollectorResult(collector="code")
    art = FileArtifact(path="services/order.py", language="python",
                       module="services.order")
    art.functions.append(FunctionDef(
        qualified_name="services.order.create", name="create",
        file=art.path, line=3, cfg={"entry": "s0", "nodes": [], "edges": []},
    ))
    art.configs.append(ConfigRef(
        key="ORDER_TIMEOUT_SECONDS", default="5", file=art.path, line=4,
    ))

    collector._emit(result, [art], set())

    function = next(n for n in result.nodes if n.type == NodeType.FUNCTION.value)
    # B5: the statement-level CFG is exported via extras/.cfg.json only.
    assert "cfg" not in function.attributes

    config = next(n for n in result.nodes if n.type == NodeType.CONFIG.value)
    # A4: keys shared with document-side Config nodes.
    assert config.attributes["value"] == "5"
    assert config.attributes["default"] == "5"
    assert config.attributes["source_file"] == art.path


# --------------------------------------------------------------------------- B6
def test_provenance_language_follows_artifact(tmp_path):
    collector = CodeCollector(_settings(tmp_path))
    result = CollectorResult(collector="code")
    art = FileArtifact(path="OrderService.java", language="java",
                       module="com.example.OrderService")
    art.functions.append(FunctionDef(
        qualified_name="com.example.OrderService.create", name="create",
        file=art.path, line=3,
    ))

    collector._emit(result, [art], set())

    function = next(n for n in result.nodes if n.type == NodeType.FUNCTION.value)
    assert function.provenance[0].detail["language"] == "java"


# --------------------------------------------------------------------------- A1 / B8
class _FakeLLM:
    def __init__(self, answer):
        self.answer = answer

    def complete_json(self, system, user):
        return self.answer


def test_document_llm_mapping_emits_all_endpoints(tmp_path, monkeypatch):
    answer = {
        "services": [{
            "service_name": "OrderService",
            "k8s_deployment": "order-service",
            "k8s_namespace": "prod",
            "source_files": [
                "services/order_service.py",
                "src/main/java/com/example/OrderService.java",
            ],
            "responsibility": "orders",
        }]
    }
    monkeypatch.setattr(
        "kg_builder.enrichment.llm_client.create_llm_client",
        lambda settings: _FakeLLM(answer),
    )
    collector = DocumentCollector(_settings(tmp_path))
    result = CollectorResult(collector="document")

    collector._llm_analyze_docs(result, [("docs/architecture.md", "OrderService")])

    node_ids = {n.id for n in result.nodes}
    assert node_ids, "LLM mapping should emit nodes"
    # A1: every edge endpoint exists, so the pipeline will not drop the facts.
    for edge in result.edges:
        assert edge.source in node_ids
        assert edge.target in node_ids

    deployments = [n for n in result.nodes if n.type == NodeType.DEPLOYMENT.value]
    modules = {n.canonical_name for n in result.nodes if n.type == NodeType.MODULE.value}
    assert deployments and deployments[0].canonical_name == "prod/order-service"
    assert modules == {"services.order_service", "com.example.OrderService"}

    # B8: model-inferred mapping is tagged as LLM provenance, not document.
    assert {p.source_type for n in result.nodes for p in n.provenance} == {
        SourceType.LLM.value
    }
    assert {e.relation for e in result.edges} == {
        RelationType.DEPLOYED_AS.value, RelationType.IMPLEMENTS.value,
    }


def test_document_llm_failure_is_reported(tmp_path):
    collector = DocumentCollector(_settings(tmp_path))
    collector._llm_analyze_text = lambda rel, text: None  # B7: simulate failure
    collector.settings.enrich = True

    result = CollectorResult(collector="document")
    collector._handle_text(
        result, Path("architecture.md"), "architecture.md", "OrderService",
    )

    assert any("using heuristic extraction" in w for w in result.warnings)
    assert any(n.type == NodeType.SERVICE.value for n in result.nodes)
