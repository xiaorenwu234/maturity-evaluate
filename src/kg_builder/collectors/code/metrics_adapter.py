"""Radon-based metrics adapter.

Maps radon's output to the project's ``artifact.Metrics`` dataclass fields.
Radon covers cyclomatic complexity, cognitive complexity (via ``radon.complexity``
and ``radon.cognitive``), LOC and MI; fields it cannot compute
(``num_fields``, ``self_data_access``, ``foreign_data_access``, control-flow
shape) are left as ``None`` here and filled by the AST-based supplement in
``ir_compat.py``.

Clone fingerprinting is intentionally separate (``clone_detector.py``) because
it uses tree-sitter rather than radon and has a different lifecycle.
"""
from __future__ import annotations

import ast
from typing import Optional

from ..code.artifact import Metrics


def _safe_cc(source: str) -> dict[str, int]:
    """Compute per-function cyclomatic complexity via radon.

    Returns ``{func_name: cc}`` for every function/method in ``source``.
    Falls back to empty dict if radon is unavailable (caller should have
    checked status first; this is defensive).
    """
    try:
        from radon.complexity import cc_visit  # type: ignore[import-untyped]
    except ImportError:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    results: dict[str, int] = {}
    for block in cc_visit(tree):
        # block.name is the function/method name; block.complexity is CC.
        # For methods radon prefixes with "ClassName." -- keep as-is so we
        # can match against FunctionDef.name later.
        results[block.name] = block.complexity
    return results


def _safe_cognitive(source: str) -> dict[str, int]:
    """Per-function cognitive complexity via radon."""
    try:
        from radon.cognitive import cognitive_complexity  # type: ignore[import-untyped]
    except ImportError:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    results: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            try:
                cc = cognitive_complexity(node)
                results[node.name] = cc
            except Exception:
                pass
    return results


def _loc_from_source(source: str) -> int:
    """Non-blank, non-comment line count (matches legacy metrics.py semantics)."""
    count = 0
    in_multiline = False
    ml_char: Optional[str] = None
    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        # Track triple-quote multiline strings/comments.
        if in_multiline:
            if ml_char and ml_char in stripped:
                in_multiline = False
            continue
        if stripped.startswith("#"):
            continue
        for q in ('"""', "'''"):
            if stripped.startswith(q):
                if stripped.count(q) == 1:
                    in_multiline = True
                    ml_char = q
                    break
                # Single-line triple-quoted string: count as code.
        else:
            count += 1
    return count


def compute_function_metrics(
    source: str, func_name: str, class_name: Optional[str] = None,
) -> Metrics:
    """Build a ``Metrics`` bag for one function/method using radon.

    Args:
        source: full file source text (radon parses the whole file internally).
        func_name: bare function name (e.g. ``create_order``).
        class_name: owning class name for methods (used to match radon's
            ``ClassName.method`` naming convention).

    Returns:
        A ``Metrics`` instance with radon-computable fields filled; others
        left ``None`` for the AST supplement in ``ir_compat.py``.
    """
    m = Metrics()

    # Cyclomatic complexity.
    cc_map = _safe_cc(source)
    lookup_keys = [func_name]
    if class_name:
        lookup_keys.append(f"{class_name}.{func_name}")
    for k in lookup_keys:
        if k in cc_map:
            m.cyclomatic_complexity = cc_map[k]
            break

    # Cognitive complexity.
    cog_map = _safe_cognitive(source)
    for k in lookup_keys:
        if k in cog_map:
            m.cognitive_complexity = cog_map[k]
            break

    # LOC is file-level in radon; per-function LOC comes from the AST
    # supplement (line_end - line + 1 of the function body). We leave it
    # None here and let ir_compat fill it.
    return m


def compute_class_metrics(source: str, class_name: str) -> Metrics:
    """Build a ``Metrics`` bag for a class.

    Radon doesn't directly compute class-level complexity; we aggregate
    method CCs as a rough proxy. Other class-level fields (num_fields,
    self/foreign access) are filled by the AST supplement.
    """
    m = Metrics()
    cc_map = _safe_cc(source)
    method_ccs = [v for k, v in cc_map.items() if k.startswith(f"{class_name}.")]
    if method_ccs:
        m.cyclomatic_complexity = sum(method_ccs)
    return m


def file_loc(source: str) -> int:
    """File-level non-blank non-comment LOC."""
    return _loc_from_source(source)
