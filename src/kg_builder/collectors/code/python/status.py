"""Availability probes for the three open-source toolkits.

Each probe is cached (lru_cache) so repeated checks within a single build are
free. Probes return ``(available: bool, detail: str)``; ``detail`` is either a
version string on success or a human-readable failure reason.

Hard-failure semantics: callers should raise when any required toolkit is
unavailable rather than falling back to the legacy channel. The project's
"no silent fallback" principle applies here verbatim.
"""
from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from typing import Tuple


@lru_cache(maxsize=1)
def scip_status() -> Tuple[bool, str]:
    """Check whether ``scip-python`` CLI is available and runnable.

    Requires both ``node`` (>=16) and the ``scip-python`` package (installed
    via npm or pip's node-binding wheel). Returns version string on success.
    """
    node = shutil.which("node")
    if not node:
        return False, "node runtime not found in PATH (required by scip-python)"
    try:
        result = subprocess.run(
            ["scip-python", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            version = (result.stdout or result.stderr).strip().splitlines()[0]
            return True, version
        return False, f"scip-python --version exited {result.returncode}: {(result.stderr or result.stdout).strip()[:200]}"
    except FileNotFoundError:
        return False, "scip-python CLI not found in PATH (install via `npm i -g @sourcegraph/scip-python` or pip scip-python)"
    except subprocess.TimeoutExpired:
        return False, "scip-python --version timed out after 10s"
    except OSError as exc:
        return False, f"scip-python invocation failed: {exc}"


@lru_cache(maxsize=1)
def radon_status() -> Tuple[bool, str]:
    """Check whether the ``radon`` Python package is importable."""
    try:
        import radon  # noqa: F401
        return True, getattr(radon, "__version__", "unknown")
    except ImportError as exc:
        return False, f"radon not installed: {exc} (pip install radon>=6.0)"


@lru_cache(maxsize=1)
def treesitter_python_status() -> Tuple[bool, str]:
    """Check whether tree-sitter + tree-sitter-python are importable and can parse."""
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_python  # noqa: F401
    except ImportError as exc:
        return False, f"tree-sitter stack missing: {exc} (pip install tree-sitter>=0.21 tree-sitter-python>=0.21)"
    # Smoke-test: parse a trivial snippet to confirm grammar loads.
    try:
        from tree_sitter import Language, Parser
        lang = Language(tree_sitter_python.language())
        parser = Parser(lang)
        tree = parser.parse(b"def x(): pass\n")
        if tree.root_node.child_count == 0:
            return False, "tree-sitter-python grammar loaded but produced empty tree"
        return True, f"tree-sitter={getattr(tree_sitter, '__version__', '?')} python={getattr(tree_sitter_python, '__version__', '?')}"
    except Exception as exc:
        return False, f"tree-sitter-python smoke parse failed: {exc}"


@lru_cache(maxsize=1)
def joern_status() -> Tuple[bool, str]:
    """Check whether the ``joern-cli`` (Joern CPG toolkit) is available."""
    path = shutil.which("joern-cli")
    if not path:
        return False, "joern-cli not found in PATH (install via https://joern.io/install/)"
    try:
        result = subprocess.run(
            ["joern-scan", "--help"], capture_output=True, text=True, timeout=30,
        )
        output = (result.stdout or result.stderr).strip()
        import re
        ver_match = re.search(r'Version:\s*`?([^\s`]+)`?', output)
        version = ver_match.group(1) if ver_match else "unknown"
        return True, f"Joern {version}"
    except FileNotFoundError:
        return False, "joern-scan not found in PATH"
    except subprocess.TimeoutExpired:
        return False, "joern-scan --help timed out after 30s"
    except OSError as exc:
        return False, f"joern invocation failed: {exc}"


def check_all() -> dict[str, Tuple[bool, str]]:
    """Run all three probes and return a combined status dict.

    Callers should inspect this before starting collection and raise a clear
    error listing every unavailable toolkit (not just the first).
    """
    return {
        "scip": scip_status(),
        "radon": radon_status(),
        "treesitter_python": treesitter_python_status(),
        "joern": joern_status(),
    }
