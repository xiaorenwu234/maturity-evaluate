"""Infrastructure-as-Code parser.

Handles Terraform (.tf.json, .tfvars.json) and Ansible playbooks.
Note: Raw HCL (.tf) is not JSON/YAML and requires regex-based extraction.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import yaml

from ....graph.model import KGEdge, KGNode, Provenance, make_node_id
from ....graph.ontology import NodeType, RelationType, SourceType
from .base import BaseConfigParser, ParseResult

logger = logging.getLogger(__name__)


class IacParser(BaseConfigParser):
    category = "iac"

    @staticmethod
    def can_parse(rel_path: str, ext: str, content: str) -> bool:
        name = rel_path.rsplit("/", 1)[-1].lower()
        # Terraform JSON variants
        if ext == ".tf.json" or name.endswith(".tf.json"):
            return True
        if name in ("main.tf.json", "variables.tf.json", "outputs.tf.json"):
            return True
        # Ansible playbooks
        if "ansible" in rel_path.lower() or "playbook" in name:
            return ext in (".yaml", ".yml")
        # Terraform HCL (basic detection)
        if ext == ".tf":
            head = content[:500]
            return "resource " in head or "provider " in head or "module " in head
        return False

    def parse(self, rel_path: str, content: str, owner_service: Optional[str],
              namespace: str) -> ParseResult:
        result = ParseResult()
        prov = Provenance(
            source_type=SourceType.DOCUMENT,
            source_id=f"doc_parser:iac:{rel_path}",
            evidence=f"IaC config from {rel_path}",
            confidence=0.85,
            detail={"parser": "iac", "file": rel_path},
        )

        ext = "." + rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""

        if ext == ".tf":
            self._parse_hcl(result, rel_path, content, namespace, prov)
        elif rel_path.lower().endswith(".tf.json"):
            self._parse_tf_json(result, rel_path, content, namespace, prov)
        else:
            self._parse_ansible(result, rel_path, content, namespace, prov)

        return result

    def _parse_hcl(self, result: ParseResult, rel_path: str, content: str,
                   namespace: str, prov: Provenance):
        """Basic HCL parsing via regex (no HCL parser dependency)."""
        # Extract resource blocks: resource "type" "name" { ... }
        resources = re.findall(r'resource\s+"([^"]+)"\s+"([^"]+)"', content)
        for rtype, rname in resources:
            node_id = make_node_id("Infrastructure", f"{rtype}/{rname}", namespace)
            result.nodes.append(KGNode(
                id=node_id, type="Infrastructure", name=rname,
                canonical_name=f"{rtype}/{rname}", namespace=namespace,
                attributes={"kind": "terraform_resource", "resource_type": rtype, "source_file": rel_path},
                provenance=[prov],
            ))

        # Extract provider blocks
        providers = re.findall(r'provider\s+"([^"]+)"', content)
        for pname in providers:
            node_id = make_node_id(NodeType.EXTERNAL_DEPENDENCY.value, f"tf-provider/{pname}", namespace)
            result.nodes.append(KGNode(
                id=node_id, type=NodeType.EXTERNAL_DEPENDENCY.value, name=pname,
                canonical_name=f"tf-provider/{pname}", namespace=namespace,
                attributes={"kind": "terraform_provider"},
                provenance=[prov],
            ))

        # Extract module blocks
        modules = re.findall(r'module\s+"([^"]+)"', content)
        for mname in modules:
            node_id = make_node_id("Infrastructure", f"tf-module/{mname}", namespace)
            result.nodes.append(KGNode(
                id=node_id, type="Infrastructure", name=mname,
                canonical_name=f"tf-module/{mname}", namespace=namespace,
                attributes={"kind": "terraform_module", "source_file": rel_path},
                provenance=[prov],
            ))

    def _parse_tf_json(self, result: ParseResult, rel_path: str, content: str,
                       namespace: str, prov: Provenance):
        """Parse Terraform JSON format."""
        try:
            data = json.loads(content)
        except Exception as exc:
            result.warnings.append(f"tf.json parse failed: {rel_path}: {exc}")
            return

        if not isinstance(data, dict):
            return

        resources = data.get("resource", {})
        if isinstance(resources, dict):
            for rtype, instances in resources.items():
                if isinstance(instances, dict):
                    for rname in instances:
                        node_id = make_node_id("Infrastructure", f"{rtype}/{rname}", namespace)
                        result.nodes.append(KGNode(
                            id=node_id, type="Infrastructure", name=rname,
                            canonical_name=f"{rtype}/{rname}", namespace=namespace,
                            attributes={"kind": "terraform_resource", "resource_type": rtype, "source_file": rel_path},
                            provenance=[prov],
                        ))

    def _parse_ansible(self, result: ParseResult, rel_path: str, content: str,
                       namespace: str, prov: Provenance):
        """Parse Ansible playbook YAML."""
        try:
            docs = list(yaml.safe_load_all(content))
        except Exception as exc:
            result.warnings.append(f"ansible parse failed: {rel_path}: {exc}")
            return

        for doc in docs:
            if not isinstance(doc, list):
                continue
            for play in doc:
                if not isinstance(play, dict):
                    continue
                play_name = play.get("name", "unnamed-play")
                hosts = play.get("hosts", "")
                tasks = play.get("tasks", [])

                node_id = make_node_id("Pipeline", f"ansible/{play_name}", namespace)
                result.nodes.append(KGNode(
                    id=node_id, type="Pipeline", name=play_name,
                    canonical_name=f"ansible/{play_name}", namespace=namespace,
                    attributes={
                        "kind": "ansible_play", "hosts": str(hosts),
                        "task_count": len(tasks) if isinstance(tasks, list) else 0,
                        "source_file": rel_path,
                    },
                    provenance=[prov],
                ))
