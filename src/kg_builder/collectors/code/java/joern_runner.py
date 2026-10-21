"""Subprocess wrapper for the Joern CPG toolkit (Java analysis).

Joern produces a Code Property Graph (CPG) from Java source, which we export
as flatgraph JSON and parse into call-edge lookup tables. This mirrors the
design of ``scip_runner.py``: batch invocation per repo, hard-failure semantics.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple


class JoernError(RuntimeError):
    """Raised when Joern operations fail. Hard-failure by contract."""


@lru_cache(maxsize=1)
def joern_status() -> Tuple[bool, str]:
    """Check whether the ``joern`` CLI is available."""
    path = shutil.which("joern-cli")
    if not path:
        return False, "joern-cli not found in PATH (install via https://joern.io/install/)"
    try:
        # Use joern-scan --help to get version info
        result = subprocess.run(
            ["joern-scan", "--help"], capture_output=True, text=True, timeout=30,
        )
        output = (result.stdout or result.stderr).strip()
        # Extract version from "Version: `X.Y.Z`" line
        import re
        ver_match = re.search(r'Version:\s*`?([^\s`]+)`?', output)
        version = ver_match.group(1) if ver_match else "unknown"
        return True, f"Joern {version}"
    except Exception as exc:
        return False, f"joern --version failed: {exc}"


def ensure_available() -> str:
    ok, detail = joern_status()
    if not ok:
        raise JoernError(f"Joern unavailable: {detail}")
    return detail


def build_cpg(repo_path: Path, output_dir: Optional[Path] = None, timeout: int = 600) -> Path:
    """Build a CPG from a Java project using Joern.

    Returns the path to the generated CPG directory/file.
    """
    ensure_available()
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise JoernError(f"repo_path is not a directory: {repo_path}")

    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="kgb_joern_"))
    cpg_path = output_dir / "cpg.bin"

    # Use joern-parse to generate CPG, then joern-export for flatgraph.
    # javasrc = source-only analysis (no compilation needed); java = requires build artifacts.
    cmd = [
        "joern-parse", str(repo_path),
        "-o", str(cpg_path),
        "--language", "javasrc",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise JoernError(f"joern-parse timed out after {timeout}s") from exc
    except OSError as exc:
        raise JoernError(f"joern-parse failed: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()[:500]
        raise JoernError(f"joern-parse exited {result.returncode}: {stderr}")

    if not cpg_path.exists():
        raise JoernError(f"joern-scan reported success but no CPG at {cpg_path}")
    return cpg_path


def export_flatgraph(cpg_path: Path, output_dir: Optional[Path] = None) -> Path:
    """Export a CPG to dot format (text-based graph representation).

    Joern 4.x exports to a directory structure with one .dot file per method.
    We return the output directory path; the parser will walk it recursively.
    """
    ensure_available()
    if output_dir is None:
        # joern-export requires the output dir to NOT exist, so we generate
        # a unique name but don't create it.
        import tempfile
        base = Path(tempfile.gettempdir()) / f"kgb_joern_dot_{id(cpg_path)}"
        output_dir = base
        # Ensure parent exists but not the dir itself
        output_dir.parent.mkdir(parents=True, exist_ok=True)

    # Set java.io.tmpdir to work around sandbox restrictions on /tmp.
    import os
    tmp_dir = output_dir.parent / f"{output_dir.name}_tmp"
    tmp_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["JAVA_OPTS"] = f"-Djava.io.tmpdir={tmp_dir} --enable-native-access=ALL-UNNAMED"

    cmd = [
        "joern-export", str(cpg_path),
        "-o", str(output_dir),
        "--repr", "cpg",
        "--format", "dot",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except Exception as exc:
        raise JoernError(f"joern-export failed: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()[:500]
        raise JoernError(f"joern-export exited {result.returncode}: {stderr}")
    return output_dir
