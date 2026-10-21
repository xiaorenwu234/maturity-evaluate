"""Subprocess wrapper for the ``scip-python`` indexer.

Mirrors the design of ``code/semgrep_runner.py``: a single batch invocation
per repository (not per file), with lru_cache'd availability probing and
explicit hard-failure semantics when the toolchain is missing or the index
cannot be produced.

Output: an ``index.scip`` protobuf file at a caller-specified path. Parsing
of that file into lookup tables lives in ``scip_resolver.py`` to keep I/O and
data-structure concerns separate.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .status import scip_status


class ScipIndexError(RuntimeError):
    """Raised when scip-python indexing fails. Hard-failure by contract."""


def ensure_available() -> str:
    """Raise ``ScipIndexError`` if scip-python is not usable; return version otherwise."""
    ok, detail = scip_status()
    if not ok:
        raise ScipIndexError(f"scip-python unavailable: {detail}")
    return detail


def index_repo(repo_path: Path, output_path: Optional[Path] = None, timeout: int = 600) -> Path:
    """Run ``scip-python python <repo> -o <output>`` and return the output path.

    Args:
        repo_path: root of the Python project to index. Must contain source
            files importable by Pyright (i.e. dependencies resolvable or at
            least stub-importable).
        output_path: where to write ``index.scip``. Defaults to a temp file
            under the system tmpdir; caller is responsible for cleanup.
        timeout: seconds before the subprocess is killed. Large repos may need
            more than the default 600s.

    Returns:
        The resolved ``output_path`` (useful when the default temp path is used).

    Raises:
        ScipIndexError: on any failure (missing tool, non-zero exit, timeout).
            This is a hard failure -- callers must not catch and degrade.
    """
    ensure_available()
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise ScipIndexError(f"repo_path is not a directory: {repo_path}")

    if output_path is None:
        fd, tmp = tempfile.mkstemp(suffix=".scip", prefix="kgb_scip_")
        os.close(fd)
        output_path = Path(tmp)
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["scip-python", "index", "--cwd", str(repo_path), "--output", str(output_path)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(repo_path),
        )
    except subprocess.TimeoutExpired as exc:
        raise ScipIndexError(
            f"scip-python timed out after {timeout}s on {repo_path}"
        ) from exc
    except OSError as exc:
        raise ScipIndexError(f"scip-python subprocess failed: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()[:500]
        raise ScipIndexError(
            f"scip-python exited {result.returncode} on {repo_path}: {stderr}"
        )
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ScipIndexError(
            f"scip-python reported success but produced no/empty index at {output_path}"
        )
    return output_path
