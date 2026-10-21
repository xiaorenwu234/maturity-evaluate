"""kg_builder -- Module 1: Data Collection & Unified System Knowledge Graph.

Public entry points:

* :class:`BuildSettings` / :func:`settings_for` -- configure a build.
* :class:`BuildPipeline` / :func:`build`        -- produce the graph.
* :mod:`kg_builder.graph`                        -- the graph model consumed by
  Module 2 (assessment) and Module 3 (remediation).
"""
from __future__ import annotations

from .config import (
    BuildSettings,
    CodeSettings,
    DeploymentSettings,
    DocumentSettings,
    LLMSettings,
    RuntimeSettings,
    settings_for,
)
from .graph import KnowledgeGraph, NodeType, Provenance, RelationType, SourceType
from .pipeline import BuildPipeline, BuildReport

__all__ = [
    "BuildSettings",
    "settings_for",
    "CodeSettings",
    "DocumentSettings",
    "DeploymentSettings",
    "RuntimeSettings",
    "LLMSettings",
    "BuildPipeline",
    "BuildReport",
    "KnowledgeGraph",
    "NodeType",
    "RelationType",
    "SourceType",
    "Provenance",
    "build",
]

__version__ = "0.1.0"


def build(repo_path, output_path="out/knowledge_graph.json", *, llm_client=None, **overrides) -> BuildReport:
    """Convenience one-call builder.

    Extra ``overrides`` map onto :class:`BuildSettings` (nested via ``__``).
    Semantic enrichment is LLM-only: either configure the model (config file / env,
    or pass ``llm_client=``), or disable it with ``enrich=False``.
    """
    settings = settings_for(repo_path, output_path, **overrides)
    return BuildPipeline(settings).run(llm_client=llm_client)
