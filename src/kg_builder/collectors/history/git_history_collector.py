"""Version-control (Git) history collector.

Turns commit history into graph facts that pure static analysis cannot see:

* **churn** -- how many commits touched a file (hotspot / maintenance-risk signal);
* **temporal coupling** -- files that repeatedly change together in the same
  commit (a Shotgun-Surgery signal), emitted as ``TEMPORALLY_COUPLED`` edges.

The module ``canonical_name`` of a node mirrors the code collector's dotted module
key so history facts merge onto the same logical ``Module`` node the source scan
already produced. Every record is tagged ``source=history`` with a conservative
confidence (0.85): observed commits are real, but the *smell inference* they
enable is probabilistic.

A non-Git repository (or a missing ``git`` binary) is not an error: the collector
records a warning and returns an empty result.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Optional

from ...config import BuildSettings
from ...graph.model import KGEdge, KGNode, Provenance, make_node_id
from ...graph.ontology import NodeType, RelationType, SourceType
from ..base import BaseCollector, CollectorResult
from ..code.languages import detect_language

_CONFIDENCE = 0.85


class HistoryCollector(BaseCollector):
    name = "history"

    def __init__(self, settings: BuildSettings) -> None:
        self.settings = settings
        self.hist = settings.history
        self.repo = Path(settings.repo_path)
        self.namespace = settings.namespace

    def is_enabled(self) -> bool:
        return self.hist.enabled and (self.repo / ".git").exists()

    # ------------------------------------------------------------------ run
    def collect(self) -> CollectorResult:
        result = CollectorResult(collector=self.name)
        commits = self._fetch_commits(result)
        if commits is None:
            return result
        result.metrics["commits_scanned"] = len(commits)

        churn: Counter[str] = Counter()          # module -> commit count touching it
        churn_files: dict[str, set[str]] = {}     # module -> contributing files
        cochange: Counter[tuple[str, str]] = Counter()  # (modA, modB) -> shared commits

        for files in commits:
            mods = sorted({m for f in files if (m := self._module_of(f))})
            for m in mods:
                churn[m] += 1
                churn_files.setdefault(m, set()).update(f for f in files if self._module_of(f) == m)
            for a, b in combinations(dict.fromkeys(mods), 2):
                cochange[(a, b)] += 1

        node_index: dict[str, KGNode] = {}
        for m, count in churn.items():
            node = self._ensure(result, node_index, m, sorted(churn_files[m]))
            node.attributes["churn_commits"] = count
            node.attributes.setdefault("files", [])
            node.attributes["files"] = sorted(set(node.attributes["files"]) | churn_files[m])

        coupled_pairs = 0
        for (a, b), weight in cochange.items():
            if weight < self.hist.min_cochange:
                continue
            na, nb = self._ensure(result, node_index, a, []), self._ensure(result, node_index, b, [])
            result.add_edge(KGEdge(na.id, nb.id, RelationType.TEMPORALLY_COUPLED.value,
                                   {"weight": weight}, [self._prov(f"git://cochange/{a}+{b}", f"{a} & {b} co-changed {weight}x")]))
            coupled_pairs += 1

        result.metrics.update(modules_with_history=len(node_index), coupled_pairs=coupled_pairs)
        return result

    # ------------------------------------------------------------ git access
    def _fetch_commits(self, result: CollectorResult) -> Optional[list[list[str]]]:
        """Return a list of per-commit changed-file lists, or ``None`` on failure."""
        cmd = ["git", "-C", str(self.repo), "log", f"-n{self.hist.max_commits}",
               "--name-only", "--pretty=format:%x00%H"]
        if self.hist.since:
            cmd += [f"--since={self.hist.since}"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:  # git missing / timeout
            result.warnings.append(f"history collector skipped: git unavailable ({exc})")
            return None
        if out.returncode != 0:
            result.warnings.append(f"history collector skipped: git log failed ({out.stderr.strip()[:200]})")
            return None
        commits: list[list[str]] = []
        for record in out.stdout.split("\0"):
            lines = [ln.strip() for ln in record.splitlines() if ln.strip()]
            if len(lines) < 2:            # header hash with no files, or preamble
                continue
            commits.append(lines[1:])      # drop the commit-hash line
        return commits

    # --------------------------------------------------------------- mapping
    def _module_of(self, rel_path: str) -> Optional[str]:
        p = Path(rel_path)
        if detect_language(p) is None:     # ignore docs / assets as coupling noise
            return None
        stem = rel_path.rsplit(".", 1)[0] if "." in p.name else rel_path
        parts = [seg for seg in stem.replace("\\", "/").split("/") if seg not in {"", "."}]
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts) or None

    def _ensure(self, result: CollectorResult, index: dict[str, KGNode], module: str, files: list[str]) -> KGNode:
        existing = index.get(module)
        if existing is not None:
            return existing
        node = KGNode(
            id=make_node_id(NodeType.MODULE.value, module, self.namespace),
            type=NodeType.MODULE.value,
            name=module,
            canonical_name=module,
            namespace=self.namespace,
            attributes={},
            provenance=[self._prov(f"git://history/{module}", f"{module} in {len(files)} file(s)")],
        )
        index[module] = node
        result.add_node(node)
        return node

    def _prov(self, source_id: str, evidence: str) -> Provenance:
        return Provenance(
            source_type=SourceType.HISTORY,
            source_id=source_id,
            evidence=evidence,
            confidence=_CONFIDENCE,
            detail={"method": "git-log"},
        )
