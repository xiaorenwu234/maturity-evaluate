"""Tests for the Git history collector (churn + temporal coupling).

A throwaway git repository is created with a handful of commits so the collector
runs against real ``git log`` output. Skipped when ``git`` is unavailable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from kg_builder.collectors.history import HistoryCollector
from kg_builder.config import settings_for
from kg_builder.graph.ontology import NodeType, RelationType, SourceType

if shutil.which("git") is None:  # pragma: no cover - environment without git
    pytest.skip("git binary required for the history collector", allow_module_level=True)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
                        "HOME": str(repo), "PATH": "/usr/bin:/bin"})


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "services").mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@e")

    a = repo / "services" / "order.py"
    b = repo / "services" / "payment.py"
    # Commit 1: both files created together -> temporal coupling.
    a.write_text("class OrderService:\n    pass\n")
    b.write_text("class PaymentService:\n    pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    # Repeated co-changes so the pair crosses the default min_cochange threshold.
    for i in range(3):
        a.write_text(f"class OrderService:\n    x = {i}\n")
        b.write_text(f"class PaymentService:\n    y = {i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", f"touch {i}")
    return repo


def _run(repo: Path):
    return HistoryCollector(settings_for(repo)).collect()


def test_history_collector_disabled_off_git_repo(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert HistoryCollector(settings_for(plain)).is_enabled() is False


def test_churn_and_coupling(git_repo: Path):
    res = _run(git_repo)
    assert res.metrics["commits_scanned"] >= 4
    mods = {n.canonical_name: n for n in res.nodes if n.type == NodeType.MODULE.value}
    assert "services.order" in mods and "services.payment" in mods
    assert mods["services.order"].attributes["churn_commits"] >= 3
    coupled = [e for e in res.edges if e.relation == RelationType.TEMPORALLY_COUPLED.value]
    assert coupled, "order+payment co-changed repeatedly, expected a coupling edge"
    assert coupled[0].attributes["weight"] >= 3


def test_history_facts_carry_history_provenance(git_repo: Path):
    res = _run(git_repo)
    for n in res.nodes:
        assert any(p.source_type == SourceType.HISTORY.value for p in n.provenance)
