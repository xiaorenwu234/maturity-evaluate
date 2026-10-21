"""Abstract protocol for call-graph provider backends.

All language-specific call-graph sources (SCIP, Joern, future CodeQL/LSP)
implement this protocol so the collector can dispatch by language without
hard-coding concrete implementations.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class CallGraphProvider(Protocol):
    """Uniform interface for any call-graph backend."""

    def is_available(self) -> bool:
        """Return True if the underlying toolkit is installed and runnable."""
        ...

    def index_repo(self, repo_path: Path) -> None:
        """Build an index / CPG for the entire repository.

        Raises on hard failure (toolkit missing, indexing error). The caller
        must not catch and degrade -- this matches the project's "no silent
        fallback" principle.
        """
        ...

    def resolve_call(
        self, file: str, line: int, callee_hint: str = "",
    ) -> Optional["ResolvedCall"]:
        """Resolve a call site to its callee.

        Returns None when the call cannot be resolved (no edge emitted).
        """
        ...

    @property
    def language(self) -> str:
        """The programming language this provider handles (e.g. 'python', 'java')."""
        ...


# Forward reference for type checking; actual class lives in scip_resolver.
from .scip_resolver import ResolvedCall as ResolvedCall  # noqa: E402,F811
