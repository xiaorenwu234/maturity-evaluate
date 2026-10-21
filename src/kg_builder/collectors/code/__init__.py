"""Open-source code analysis collection layer.

Replaces the legacy ``code/`` collector's hand-rolled name-based resolution,
metrics and CFG with precise open-source tooling:

* **scip-python** -- symbol-level definition/reference index (replaces SymbolIndex.resolve)
* **radon** -- cyclomatic / cognitive / LOC metrics (replaces metrics.py)
* **tree-sitter-python** -- statement-level CFG + clone fingerprint (replaces cfg.py / fingerprint)

The framework-semantic channel (routes/resources/configs via Semgrep) is reused
unchanged from ``code/frameworks.py``. The output contract (``FileArtifact`` /
``CollectorResult``) is identical to the legacy collector so downstream _emit,
pipeline fusion and evaluation layers require zero changes.

Hard-failure semantics: if any of the three toolkits is unavailable, collection
aborts with a clear error rather than silently degrading. This matches the
project's "no silent fallback" principle and the user-confirmed decision that
the module must be a complete replacement, not a layered fallback.
"""
from .collector import CodeCollector

__all__ = ["CodeCollector"]
