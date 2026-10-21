"""Build FileArtifact from Java source + Joern call index.

Uses tree-sitter-java for structural extraction (classes, functions, imports)
and Joern's CPG for precise call resolution. Mirrors the Python ir_compat.py
design but adapted for Java syntax.
"""
from __future__ import annotations

import re
from typing import Optional

from ..schema.artifact import (
    ArgHint, CallRef, ClassDef, FileArtifact, FunctionDef, ImportRef, Metrics,
)
from .cpg_parser import JavaCallIndex, resolve_java_call
from .cfg_builder_java import build_java_cfg


def _java_module_of(rel_path: str) -> str:
    """Convert a Java file path to a dotted module name.

    ``src/main/java/com/example/OrderService.java`` -> ``com.example.OrderService``
    """
    # Strip common Java source roots.
    p = rel_path.replace("\\", "/")
    for prefix in ("src/main/java/", "src/test/java/", "src/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    return p.replace("/", ".").removesuffix(".java")


def _compute_java_cc(method_node) -> int:
    """Compute cyclomatic complexity for a Java method.

    CC = 1 + number of decision points (if, while, for, case, catch, &&, ||).
    """
    cc = 1
    _count_decisions(method_node, cc_counter=[cc])
    return cc_counter[0] if (cc_counter := [cc]) else 1


def _count_decisions(node, cc_counter: list[int]):
    """Recursively count decision points in a Java AST."""
    t = node.type
    # Decision points that increase CC
    if t in ("if_statement", "while_statement", "for_statement",
             "catch_clause", "conditional_expression",  # ternary
             "switch_label"):  # each case/default
        cc_counter[0] += 1
    # Logical operators (&& and ||)
    elif t == "binary_expression":
        op = node.child_by_field_name("operator")
        if op and op.text.decode("utf-8") in ("&&", "||"):
            cc_counter[0] += 1

    for child in node.children:
        _count_decisions(child, cc_counter)


def _count_java_branches(method_node) -> int:
    """Count the number of branch statements in a Java method."""
    count = [0]
    _count_branch_nodes(method_node, count)
    return count[0]


def _count_branch_nodes(node, count: list[int]):
    """Recursively count branch nodes."""
    if node.type in ("if_statement", "while_statement", "for_statement",
                     "switch_statement", "try_statement"):
        count[0] += 1
    for child in node.children:
        _count_branch_nodes(child, count)


def _compute_max_nesting(method_node) -> int:
    """Compute maximum nesting depth in a Java method."""
    return _nesting_depth(method_node, 0)


def _nesting_depth(node, current_depth: int) -> int:
    """Recursively compute max nesting depth."""
    max_depth = current_depth
    # Blocks increase nesting
    if node.type == "block":
        current_depth += 1
        max_depth = max(max_depth, current_depth)

    for child in node.children:
        child_depth = _nesting_depth(child, current_depth)
        max_depth = max(max_depth, child_depth)

    return max_depth


def build_java_artifact(
    path: str, source: str, module: str, call_index: JavaCallIndex,
) -> FileArtifact:
    """Build a FileArtifact for one Java file using tree-sitter + Joern.

    When Joern CPG is unavailable, falls back to tree-sitter-only extraction
    with simple heuristic call resolution (method name matching within the
    same file and across files via the call_index's methods_by_name table).
    """
    art = FileArtifact(path=path, language="java", module=module)

    try:
        from tree_sitter import Language, Parser  # type: ignore[import-untyped]
        import tree_sitter_java  # type: ignore[import-untyped]
        lang = Language(tree_sitter_java.language())
        parser = Parser(lang)
        tree = parser.parse(source.encode("utf-8"))
    except ImportError:
        return art  # tree-sitter-java not installed; return empty artifact.

    lines = source.splitlines()

    # First pass: collect all method definitions in this file for local resolution.
    local_methods: dict[str, str] = {}  # bare_name -> full_qname
    _collect_methods(tree.root_node, module, local_methods)

    # Walk the CST to extract classes, methods, imports, and calls.
    _walk_node(tree.root_node, art, module, path, lines, call_index, depth=0, local_methods=local_methods, source=source)

    return art


def _collect_methods(node, module: str, local_methods: dict[str, str]):
    """Collect all method definitions in the file for local call resolution."""
    if node.type == "method_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            method_name = name_node.text.decode("utf-8")
            # Find enclosing class.
            class_name = None
            parent = node.parent
            while parent:
                if parent.type == "class_declaration":
                    cn = parent.child_by_field_name("name")
                    class_name = cn.text.decode("utf-8") if cn else None
                    break
                parent = parent.parent
            qname = f"{module}.{class_name}.{method_name}" if class_name else f"{module}.{method_name}"
            local_methods[method_name] = qname

    for child in node.children:
        _collect_methods(child, module, local_methods)


def _walk_node(node, art, module, path, lines, call_index, depth, local_methods=None, source=None):
    """Recursively walk tree-sitter-java CST nodes."""
    if local_methods is None:
        local_methods = {}
    t = node.type

    if t == "class_declaration":
        name_node = node.child_by_field_name("name")
        class_name = name_node.text.decode("utf-8") if name_node else "Unknown"
        qname = f"{module}.{class_name}"
        line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        # Extract superclass.
        bases = []
        superclass = node.child_by_field_name("superclass")
        if superclass:
            bases.append(superclass.text.decode("utf-8"))

        # Extract methods within this class.
        methods = []
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                if child.type == "method_declaration":
                    mname_node = child.child_by_field_name("name")
                    mname = mname_node.text.decode("utf-8") if mname_node else "unknown"
                    methods.append(mname)

        loc = max(0, end_line - line + 1)
        art.classes.append(ClassDef(
            qualified_name=qname, name=class_name, file=path, line=line,
            bases=bases, methods=methods, line_end=end_line,
            metrics=Metrics(loc=loc),
        ))

    elif t == "method_declaration":
        name_node = node.child_by_field_name("name")
        method_name = name_node.text.decode("utf-8") if name_node else "unknown"
        line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        # Find enclosing class.
        class_name = None
        parent = node.parent
        while parent:
            if parent.type == "class_declaration":
                cn = parent.child_by_field_name("name")
                class_name = cn.text.decode("utf-8") if cn else None
                break
            parent = parent.parent

        qname = f"{module}.{class_name}.{method_name}" if class_name else f"{module}.{method_name}"

        # Params.
        params_node = node.child_by_field_name("parameters")
        params = []
        if params_node:
            for child in params_node.children:
                if child.type == "formal_parameter":
                    pname = child.child_by_field_name("name")
                    if pname:
                        params.append(pname.text.decode("utf-8"))

        is_async = False  # Java doesn't have async keyword like Python.
        loc = max(0, end_line - line + 1)

        # Compute enhanced metrics: cyclomatic complexity, branches, nesting.
        method_source = node.text.decode("utf-8")
        cc = _compute_java_cc(node)
        num_branches = _count_java_branches(node)
        max_nesting = _compute_max_nesting(node)

        # Build CFG for this method.
        cfg = None
        try:
            cfg = build_java_cfg(source.encode("utf-8"), method_name)
        except Exception:
            pass  # CFG building failed; leave as None.

        art.functions.append(FunctionDef(
            qualified_name=qname, name=method_name, file=path, line=line,
            class_name=class_name, params=params, is_async=is_async,
            line_end=end_line,
            metrics=Metrics(
                loc=loc, num_params=len(params),
                cyclomatic_complexity=cc, num_branches=num_branches,
                max_nesting_depth=max_nesting,
            ),
            cfg=cfg,
        ))

    elif t == "import_declaration":
        text = node.text.decode("utf-8").strip().rstrip(";")
        # "import com.example.Foo;" -> module="com.example", names=["Foo"]
        m = re.match(r"import\s+(static\s+)?(.+)", text)
        if m:
            imp_path = m.group(2).strip()
            parts = imp_path.rsplit(".", 1)
            mod = parts[0] if len(parts) > 1 else ""
            names = [parts[1]] if len(parts) > 1 else [imp_path]
            art.imports.append(ImportRef(module=mod, names=names, file=path, line=node.start_point[0] + 1))

    elif t == "method_invocation":
        line = node.start_point[0] + 1
        name_node = node.child_by_field_name("name")
        callee_text = name_node.text.decode("utf-8") if name_node else ""

        # Build full callee expression (e.g. "obj.method" or "Class.method").
        obj_node = node.child_by_field_name("object")
        if obj_node:
            full_callee = f"{obj_node.text.decode('utf-8')}.{callee_text}"
        else:
            full_callee = callee_text

        # Find enclosing method for caller_qname.
        caller_qname = None
        parent = node.parent
        while parent:
            if parent.type == "method_declaration":
                mn = parent.child_by_field_name("name")
                mn_text = mn.text.decode("utf-8") if mn else ""
                # Find enclosing class.
                pp = parent.parent
                while pp:
                    if pp.type == "class_declaration":
                        cn = pp.child_by_field_name("name")
                        cn_text = cn.text.decode("utf-8") if cn else ""
                        caller_qname = f"{module}.{cn_text}.{mn_text}"
                        break
                    pp = pp.parent
                if not caller_qname:
                    caller_qname = f"{module}.{mn_text}"
                break
            parent = parent.parent

        # Try Joern resolution first, then fall back to local method matching.
        resolved = resolve_java_call(call_index, path, line, callee_text)
        if not resolved and callee_text in local_methods:
            # Local heuristic: match by bare method name within this file.
            from .cpg_parser import ResolvedJavaCall
            resolved = ResolvedJavaCall(
                callee_qname=local_methods[callee_text],
                caller_qname=caller_qname,
            )
        call_ref = CallRef(
            caller_qname=caller_qname, callee_name=full_callee,
            file=path, line=line, args=[],
            caller_class=None,
        )
        if resolved:
            call_ref._scip_resolved = resolved  # type: ignore[attr-defined]
        art.calls.append(call_ref)

    # Recurse into children.
    for child in node.children:
        _walk_node(child, art, module, path, lines, call_index, depth + 1, local_methods, source)
