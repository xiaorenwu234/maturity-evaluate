"""Regression tests for the performance / cost fixes (C1-C3).

* C1 Semgrep must receive ``code.exclude_dirs`` instead of scanning vendor trees.
* C2 the code collector's walk must prune excluded directories and use one pass.
* C3 document LLM work is batched and the global prompt is character-budgeted.
"""

from __future__ import annotations

import re
from pathlib import Path

from kg_builder.collectors.base import CollectorResult
from kg_builder.collectors.code.collector import CodeCollector
from kg_builder.collectors.code.enrichment import frameworks
from kg_builder.collectors.document.doc_collector import (
    DocumentCollector,
    _LLM_TOTAL_DOCS_CHARS,
)
from kg_builder.config import settings_for


# --------------------------------------------------------------------------- C1
def test_enrich_all_forwards_exclude_dirs(monkeypatch):
    captured = {}

    monkeypatch.setattr(frameworks, "semgrep_status", lambda: (True, "test"))
    monkeypatch.setattr(
        frameworks, "scan_repo",
        lambda repo, rules, **kwargs: captured.update(kwargs) or [],
    )

    status = frameworks.enrich_all([], Path("."), ["node_modules", "vendor"])

    assert status["ok"] is True
    assert captured["extra_excludes"] == ["node_modules", "vendor"]


# --------------------------------------------------------------------------- C2
def test_code_walk_prunes_excluded_dirs(tmp_path):
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "vendored.py").write_text("x = 1\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n")

    collector = CodeCollector(settings_for(tmp_path, tmp_path / "kg.json", enrich=False))
    files = collector._iter_source_files()

    assert {p.relative_to(tmp_path).as_posix() for p in files} == {"app/main.py"}


# --------------------------------------------------------------------------- C3
class _BatchLLM:
    """Returns batch-shaped answers and counts requests."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        rels = re.findall(r"=== FILE: (.+?) ===\n", user)
        if "多篇技术文档" in system:
            return {"documents": {
                rel: {"services": [{"name": "OrderService", "responsibility": "orders"}],
                      "summary": f"summary of {rel}"}
                for rel in rels
            }}
        if "多个配置文件" in system:
            return {"files": {
                rel: {"category": "app_config", "owner_service": "OrderService",
                      "summary": "config"}
                for rel in rels
            }}
        return {"services": []}


def test_document_llm_calls_are_batched(tmp_path, monkeypatch):
    for i in range(5):
        (tmp_path / f"doc{i}.md").write_text(f"# doc {i}\n\nOrderService calls PaymentService.\n")
        (tmp_path / f"cfg{i}.yaml").write_text(f"retry: {i}\n")

    fake = _BatchLLM()
    monkeypatch.setattr(
        "kg_builder.enrichment.llm_client.create_llm_client", lambda settings: fake,
    )

    settings = settings_for(
        tmp_path, tmp_path / "kg.json", code_analysis__enabled=False,
        history__enabled=False,
    )
    result = DocumentCollector(settings).collect()

    # 5 docs -> 1 batch, 5 configs -> 1 batch, 1 global mapping call.
    assert fake.calls == 3
    assert result.metrics["documents_scanned"] == 10


def test_global_document_prompt_respects_budget():
    result = CollectorResult(collector="document")
    docs = [(f"doc{i}.md", "x" * 20_000) for i in range(3)]

    payload = DocumentCollector._docs_payload(result, docs)

    assert len(payload) <= _LLM_TOTAL_DOCS_CHARS
    assert any("truncated" in w for w in result.warnings)
