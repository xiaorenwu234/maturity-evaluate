"""Unit tests for Java CPG parser and IR adapter.

Integration tests that require Joern are marked skipif.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path


def _joern_available() -> bool:
    try:
        from kg_builder.collectors.code.java.joern_runner import joern_status
        ok, _ = joern_status()
        return ok
    except Exception:
        return False


def _ts_java_available() -> bool:
    try:
        import tree_sitter_java  # noqa: F401
        return True
    except ImportError:
        return False


class TestCpgParser:
    """Unit tests for dot export parsing (no Joern needed)."""

    def test_parse_empty_dot_dir(self, tmp_path):
        from kg_builder.collectors.code.java.cpg_parser import parse_dot_export
        idx = parse_dot_export(tmp_path)
        assert len(idx.methods_by_name) == 0
        assert len(idx.calls_by_pos) == 0

    def test_parse_method_nodes(self, tmp_path):
        from kg_builder.collectors.code.java.cpg_parser import parse_dot_export
        # Create a mock dot file with METHOD node
        dot_dir = tmp_path / "com" / "example"
        dot_dir.mkdir(parents=True)
        dot_file = dot_dir / "test.dot"
        dot_file.write_text('''digraph {
  "1" [label="METHOD" FULL_NAME="com.example.OrderService.createOrder:void" NAME="createOrder" FILENAME="OrderService.java" LINE_NUMBER="10" LINE_NUMBER_END="25"];
}
''')
        idx = parse_dot_export(tmp_path)
        assert len(idx.methods_by_name) == 1
        assert "com.example.OrderService.createOrder" in idx.methods_by_name
        m = idx.methods_by_name["com.example.OrderService.createOrder"]
        assert m.name == "createOrder"
        assert m.line == 10
        assert m.end_line == 25

    def test_parse_call_edges(self, tmp_path):
        from kg_builder.collectors.code.java.cpg_parser import parse_dot_export
        # Create a mock dot file with METHOD and CALL nodes
        dot_dir = tmp_path / "com" / "example"
        dot_dir.mkdir(parents=True)
        dot_file = dot_dir / "test.dot"
        dot_file.write_text('''digraph {
  "1" [label="METHOD" FULL_NAME="com.example.OrderService.createOrder:void" NAME="createOrder" FILENAME="OrderService.java" LINE_NUMBER="10" LINE_NUMBER_END="25"];
  "2" [label="CALL" METHOD_FULL_NAME="com.example.PaymentService.pay:void" NAME="pay" LINE_NUMBER="20"];
  "1" -> "2" [label="CONTAINS"];
}
''')
        idx = parse_dot_export(tmp_path)
        calls = idx.get_calls_at("OrderService.java", 20)
        assert len(calls) == 1
        assert calls[0].callee_full_name == "com.example.PaymentService.pay"
        assert calls[0].caller_full_name == "com.example.OrderService.createOrder"

    def test_resolve_java_call_with_tolerance(self, tmp_path):
        from kg_builder.collectors.code.java.cpg_parser import parse_dot_export, resolve_java_call
        # Create mock dot file with METHOD and CALL
        dot_dir = tmp_path / "com" / "example"
        dot_dir.mkdir(parents=True)
        dot_file = dot_dir / "test.dot"
        dot_file.write_text('''digraph {
  "1" [label="METHOD" FULL_NAME="Svc.doWork:void" NAME="doWork" FILENAME="Svc.java" LINE_NUMBER="10" LINE_NUMBER_END="30"];
  "2" [label="CALL" METHOD_FULL_NAME="Util.helper:void" NAME="helper" LINE_NUMBER="16"];
  "1" -> "2" [label="CONTAINS"];
}
''')
        idx = parse_dot_export(tmp_path)
        # Exact line match.
        r = resolve_java_call(idx, "Svc.java", 16, "helper")
        assert r is not None and r.callee_qname == "Util.helper"
        # ±1 tolerance.
        r2 = resolve_java_call(idx, "Svc.java", 15, "helper")
        assert r2 is not None and r2.callee_qname == "Util.helper"

    def test_clean_java_qname_strips_return_type(self):
        from kg_builder.collectors.code.java.cpg_parser import _clean_java_qname
        assert _clean_java_qname("com.Foo.bar:void") == "com.Foo.bar"
        assert _clean_java_qname("com.Foo.bar") == "com.Foo.bar"


class TestJavaIrAdapter:
    """Unit tests for Java artifact building (tree-sitter-java required)."""

    @pytest.mark.skipif(
        not _ts_java_available(), reason="tree-sitter-java not installed",
    )
    def test_build_java_artifact_basic(self):
        from kg_builder.collectors.code.java.java_ir_adapter import build_java_artifact
        from kg_builder.collectors.code.java.cpg_parser import JavaCallIndex
        src = """package com.example;

public class OrderService {
    public void createOrder(String id) {
        System.out.println(id);
    }
}
"""
        art = build_java_artifact("OrderService.java", src, "com.example.OrderService", JavaCallIndex())
        assert len(art.classes) == 1
        assert art.classes[0].name == "OrderService"
        assert len(art.functions) == 1
        assert art.functions[0].name == "createOrder"
        assert art.functions[0].class_name == "OrderService"

    @pytest.mark.skipif(
        not _ts_java_available(), reason="tree-sitter-java not installed",
    )
    def test_java_imports_extracted(self):
        from kg_builder.collectors.code.java.java_ir_adapter import build_java_artifact
        from kg_builder.collectors.code.java.cpg_parser import JavaCallIndex
        src = "import java.util.List;\nimport com.example.Foo;\n\npublic class Bar {}\n"
        art = build_java_artifact("Bar.java", src, "Bar", JavaCallIndex())
        assert len(art.imports) == 2
        modules = {i.module for i in art.imports}
        assert "java.util" in modules
        assert "com.example" in modules


@pytest.mark.skipif(not _joern_available(), reason="Joern not installed")
class TestJoernIntegration:
    """End-to-end tests requiring Joern CLI."""

    def test_joern_status_ok(self):
        from kg_builder.collectors.code.java.joern_runner import joern_status
        ok, detail = joern_status()
        assert ok, f"Joern should be available: {detail}"

