"""Type-2 clone fingerprinting via tree-sitter-python.

Reproduces the legacy ``code/metrics.fingerprint()`` algorithm (identifiers
and literals normalised to ``N``/``L`` placeholders, SHA1 truncated to 16 hex
chars) but driven by tree-sitter CST traversal instead of stdlib ``ast``.
This keeps the module on a single parser stack and produces
fingerprints bitwise-compatible with the legacy channel so historical
clone-group data remains comparable.

Threshold constant ``CLONE_MIN_TOKENS`` matches the legacy value (15) so
CLONE_OF edge emission criteria are unchanged.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

try:
    from tree_sitter import Language, Parser, Node  # type: ignore[import-untyped]
    import tree_sitter_python  # type: ignore[import-untyped]
    _LANG = Language(tree_sitter_python.language())
except ImportError:
    _LANG = None  # Deferred: functions raise at call time if unavailable.


CLONE_MIN_TOKENS = 15  # matches legacy code/metrics.py threshold


# Node types whose text should be replaced by a placeholder.
_IDENTIFIER_TYPES = {"identifier", "attribute"}
_LITERAL_TYPES = {
    "integer", "float", "string", "concatenated_string",
    "true", "false", "none",
}


def _normalise_tokens(node: Node) -> list[str]:
    """Walk the CST and yield a normalised token stream.

    Identifiers -> ``N``, literals -> ``L``, everything else kept verbatim
    (keywords, operators, punctuation). Whitespace and comments are skipped.
    """
    tokens: list[str] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.child_count == 0:
            t = cur.type
            text = cur.text.decode("utf-8", errors="replace")
            if t in _IDENTIFIER_TYPES:
                tokens.append("N")
            elif t in _LITERAL_TYPES:
                tokens.append("L")
            elif t in ("comment", "newline", "escape_sequence"):
                continue
            else:
                tokens.append(text)
        else:
            # Push children in reverse so leftmost is processed first.
            stack.extend(reversed(cur.children))
    return tokens


def fingerprint(source: bytes | str, func_name: str) -> tuple[Optional[str], int]:
    """Compute the type-2 clone fingerprint for a named function.

    Returns ``(hash_hex, token_count)`` or ``(None, 0)`` if the function
    cannot be found or parsing fails. ``hash_hex`` is the first 16 chars of
    the SHA1 of the normalised token stream joined by spaces.
    """
    if _LANG is None:
        raise ImportError(
            "tree-sitter stack required for clone detection. "
            "Install via `pip install tree-sitter>=0.21 tree-sitter-python>=0.21`."
        )
    if isinstance(source, str):
        source = source.encode("utf-8")
    parser = Parser(_LANG)
    tree = parser.parse(source)

    target: Optional[Node] = None
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "function_definition" and node.child_by_field_name("name"):
            name_node = node.child_by_field_name("name")
            if name_node and name_node.text.decode("utf-8", errors="replace") == func_name:
                target = node
                break
        stack.extend(reversed(node.children))

    if target is None:
        return None, 0

    body = target.child_by_field_name("body")
    if body is None:
        return None, 0

    tokens = _normalise_tokens(body)
    if not tokens:
        return None, 0

    digest = hashlib.sha1(" ".join(tokens).encode("utf-8")).hexdigest()[:16]
    return digest, len(tokens)


def detect_clones(
    functions: list[tuple[str, str, bytes | str]],
) -> list[tuple[str, str, int]]:
    """Batch fingerprint a list of functions and group by hash.

    Args:
        functions: list of ``(qualified_name, bare_name, source_bytes)`` tuples.

    Returns:
        List of ``(qualified_name, clone_hash, token_count)`` for every
        function whose fingerprint meets ``CLONE_MIN_TOKENS``. Callers
        (collector._emit) group by hash and emit CLONE_OF edges when a
        group has >=2 distinct definitions.
    """
    results: list[tuple[str, str, int]] = []
    for qname, bare, src in functions:
        h, n = fingerprint(src, bare)
        if h and n >= CLONE_MIN_TOKENS:
            results.append((qname, h, n))
    return results
