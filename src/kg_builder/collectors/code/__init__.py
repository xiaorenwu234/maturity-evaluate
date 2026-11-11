"""Open-source code analysis collection layer.

Replaces the legacy ``code/`` collector's hand-rolled name-based resolution,
metrics and CFG with precise open-source tooling:

* **scip-python** -- symbol-level definition/reference index (replaces SymbolIndex.resolve)
* **radon** -- cyclomatic / cognitive / LOC metrics (replaces metrics.py)
* **tree-sitter-python** -- statement-level CFG + clone fingerprint (replaces cfg.py / fingerprint)

Java uses the same IR through tree-sitter-java: structure and CFG come from the
grammar, call resolution comes from Joern's CPG. A call Joern cannot resolve
stays unresolved (no name-matching fallback).

The framework-semantic channel (routes/resources/configs via Semgrep) is reused
unchanged from ``code/frameworks.py``. The output contract (``FileArtifact`` /
``CollectorResult``) is identical to the legacy collector so downstream _emit,
pipeline fusion and evaluation layers require zero changes.

Hard-failure semantics: if any toolkit required for a detected language is
unavailable, collection aborts with a clear error rather than silently
degrading. That includes tree-sitter-java when ``code_analysis.java_enabled``.
This matches the project's "no silent fallback" principle and the user-confirmed
decision that the module must be a complete replacement, not a layered fallback.
"""
from .collector import CodeCollector

__all__ = ["CodeCollector"]
