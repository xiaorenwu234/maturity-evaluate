"""CI/CD pipeline configuration parser.

Handles GitHub Actions, GitLab CI, Jenkinsfile, CircleCI configs.
"""
from __future__ import annotations

import logging
from typing import Optional

import yaml

from ....graph.model import KGEdge, KGNode, Provenance, make_node_id
from ....graph.ontology import NodeType, RelationType, SourceType
from .base import BaseConfigParser, ParseResult

logger = logging.getLogger(__name__)


class CIParser(BaseConfigParser):
    category = "ci"

    @staticmethod
    def can_parse(rel_path: str, ext: str, content: str) -> bool:
        rp = rel_path.lower()
        name = rp.rsplit("/", 1)[-1]
        # GitHub Actions
        if ".github/workflows/" in rp and ext in (".yaml", ".yml"):
            return True
        # GitLab CI
        if name == ".gitlab-ci.yml":
            return True
        # CircleCI
        if ".circleci/config" in rp:
            return True
        # Jenkinsfile
        if name.startswith("jenkinsfile"):
            return True
        return False

    def parse(self, rel_path: str, content: str, owner_service: Optional[str],
              namespace: str) -> ParseResult:
        result = ParseResult()
        prov = Provenance(
            source_type=SourceType.DOCUMENT,
            source_id=f"doc_parser:ci:{rel_path}",
            evidence=f"CI/CD config from {rel_path}",
            confidence=0.9,
            detail={"parser": "ci", "file": rel_path},
        )

        rp = rel_path.lower()
        name = rp.rsplit("/", 1)[-1]

        if name.startswith("jenkinsfile"):
            self._parse_jenkinsfile(result, rel_path, content, namespace, prov)
        elif ext in (".yaml", ".yml"):
            self._parse_yaml_ci(result, rel_path, content, namespace, prov)

        return result

    def _parse_yaml_ci(self, result: ParseResult, rel_path: str, content: str,
                       namespace: str, prov: Provenance):
        try:
            data = yaml.safe_load(content)
        except Exception as exc:
            result.warnings.append(f"CI YAML parse failed: {rel_path}: {exc}")
            return

        if not isinstance(data, dict):
            return

        rp = rel_path.lower()
        jobs = {}
        stages = []

        # GitHub Actions
        if ".github/workflows/" in rp:
            jobs = data.get("jobs", {})
            wf_name = data.get("name", rel_path)
            node_id = make_node_id("Pipeline", wf_name, namespace)
            result.nodes.append(KGNode(
                id=node_id, type="Pipeline", name=wf_name,
                canonical_name=wf_name, namespace=namespace,
                attributes={"kind": "github_actions", "job_count": len(jobs), "source_file": rel_path},
                provenance=[prov],
            ))

        # GitLab CI
        elif ".gitlab-ci" in rp:
            stages = data.get("stages", [])
            jobs = {k: v for k, v in data.items() if isinstance(v, dict) and "stage" in v}
            node_id = make_node_id("Pipeline", "gitlab-ci", namespace)
            result.nodes.append(KGNode(
                id=node_id, type="Pipeline", name="gitlab-ci",
                canonical_name="gitlab-ci", namespace=namespace,
                attributes={"kind": "gitlab_ci", "stages": stages, "job_count": len(jobs), "source_file": rel_path},
                provenance=[prov],
            ))

        # CircleCI
        elif ".circleci" in rp:
            jobs = data.get("jobs", {})
            workflows = data.get("workflows", {})
            node_id = make_node_id("Pipeline", "circleci", namespace)
            result.nodes.append(KGNode(
                id=node_id, type="Pipeline", name="circleci",
                canonical_name="circleci", namespace=namespace,
                attributes={"kind": "circleci", "job_count": len(jobs),
                            "workflow_count": len(workflows), "source_file": rel_path},
                provenance=[prov],
            ))

        # Create nodes for individual jobs
        if isinstance(jobs, dict):
            for job_name, job_config in list(jobs.items())[:50]:
                job_id = make_node_id("Pipeline", f"{rel_path}/{job_name}", namespace)
                needs = []
                if isinstance(job_config, dict):
                    needs = job_config.get("needs", job_config.get("dependencies", []))
                    if isinstance(needs, str):
                        needs = [needs]
                result.nodes.append(KGNode(
                    id=job_id, type="Pipeline", name=job_name,
                    canonical_name=f"{rel_path}/{job_name}", namespace=namespace,
                    attributes={"kind": "ci_job", "source_file": rel_path},
                    provenance=[prov],
                ))

    def _parse_jenkinsfile(self, result: ParseResult, rel_path: str, content: str,
                           namespace: str, prov: Provenance):
        """Basic Jenkinsfile parsing via regex (Groovy is not YAML/JSON)."""
        import re
        stages = re.findall(r"stage\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", content)
        node_id = make_node_id("Pipeline", "jenkins", namespace)
        result.nodes.append(KGNode(
            id=node_id, type="Pipeline", name="jenkins",
            canonical_name="jenkins", namespace=namespace,
            attributes={"kind": "jenkins", "stages": stages, "source_file": rel_path},
            provenance=[prov],
        ))
