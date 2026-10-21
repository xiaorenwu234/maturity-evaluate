"""Language detection and Tree-sitter availability.

The code collector is built around a pluggable extractor interface:

* Python gets a first-class extractor based on the standard-library ``ast``
  module -- zero extra dependencies and the most accurate call/import resolution.
* Other languages (JS/TS/Java/Go...) are handled by a Tree-sitter extractor
  when the optional ``tree-sitter`` + ``tree-sitter-language-pack`` packages are
  installed and a grammar for that language exists. If either is missing, the
  file is skipped -- there is no lower-fidelity regex fallback.

This mirrors the recommended stack: ``AST / Tree-sitter + static call analysis +
LLM semantic enrichment``.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

# Extension -> language key.
EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".scala": "scala",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".rs": "rust",
}

# Files whose name strongly implies a particular language even without an ext.
SPECIAL_FILENAMES: dict[str, str] = {
    "Dockerfile": "dockerfile",
    "Gemfile": "ruby",
    "go.mod": "go",
    "pom.xml": "java",
}


def detect_language(path) -> Optional[str]:
    """Return a language key for a file path, or ``None`` if not a source file."""
    from pathlib import Path

    p = Path(path)
    name = p.name
    if name in SPECIAL_FILENAMES:
        return SPECIAL_FILENAMES[name]
    return EXT_TO_LANGUAGE.get(p.suffix.lower())


def is_source_file(path) -> bool:
    return detect_language(path) is not None


@lru_cache(maxsize=1)
def treesitter_status() -> tuple[bool, str]:
    """Check whether Tree-sitter + language pack are importable.

    Returns ``(available, message)``. Cached because it only depends on the
    installed environment, not on any input.
    """
    try:  # pragma: no cover - depends on optional install
        import tree_sitter  # type: ignore  # noqa: F401
        from tree_sitter_language_pack import get_language  # type: ignore  # noqa: F401

        return True, "tree-sitter available"
    except Exception as exc:  # noqa: BLE001
        return False, f"tree-sitter unavailable ({exc.__class__.__name__}: {exc})"
