"""Docker configuration parser.

Handles Dockerfile and docker-compose.yaml files.
Extracts container images, exposed ports, volumes, environment variables.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import yaml

from ....graph.model import KGEdge, KGNode, Provenance, make_node_id
from ....graph.ontology import NodeType, RelationType, SourceType
from .base import BaseConfigParser, ParseResult

logger = logging.getLogger(__name__)


class DockerParser(BaseConfigParser):
    category = "docker"

    @staticmethod
    def can_parse(rel_path: str, ext: str, content: str) -> bool:
        name = rel_path.rsplit("/", 1)[-1].lower()
        # Dockerfile variants
        if name.startswith("dockerfile"):
            return True
        # docker-compose variants
        if "docker-compose" in name or "compose.y" in name:
            return ext in (".yaml", ".yml")
        return False

    def parse(self, rel_path: str, content: str, owner_service: Optional[str],
              namespace: str) -> ParseResult:
        result = ParseResult()
        name = rel_path.rsplit("/", 1)[-1].lower()

        prov = Provenance(
            source_type=SourceType.DOCUMENT,
            source_id=f"doc_parser:docker:{rel_path}",
            evidence=f"Docker config from {rel_path}",
            confidence=0.9,
            detail={"parser": "docker", "file": rel_path},
        )

        if name.startswith("dockerfile"):
            self._parse_dockerfile(result, rel_path, content, owner_service, namespace, prov)
        else:
            self._parse_compose(result, rel_path, content, owner_service, namespace, prov)

        return result

    def _parse_dockerfile(self, result: ParseResult, rel_path: str, content: str,
                          owner_service: Optional[str], namespace: str, prov: Provenance):
        """Parse Dockerfile instructions."""
        base_image = None
        ports = []
        env_vars = {}
        cmd = None

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            upper = line.upper()
            if upper.startswith("FROM "):
                base_image = line.split(None, 1)[1].split()[0]  # first token after FROM
            elif upper.startswith("EXPOSE "):
                ports.extend(line.split(None, 1)[1].split())
            elif upper.startswith("ENV "):
                parts = line.split(None, 2)
                if len(parts) >= 3:
                    env_vars[parts[1]] = parts[2][:120]
            elif upper.startswith("CMD ") or upper.startswith("ENTRYPOINT "):
                cmd = line.split(None, 1)[1][:200]

        container_name = owner_service or rel_path.replace("/", "_").replace(".", "_")
        node_id = make_node_id("Container", container_name, namespace)
        result.nodes.append(KGNode(
            id=node_id, type="Container", name=container_name,
            canonical_name=container_name, namespace=namespace,
            attributes={
                "base_image": base_image, "ports": ports,
                "env_vars": env_vars, "cmd": cmd, "source_file": rel_path,
            },
            provenance=[prov],
        ))

        # Link to service
        if owner_service:
            svc_id = make_node_id(NodeType.SERVICE.value, owner_service, namespace)
            result.edges.append(KGEdge(svc_id, node_id, RelationType.DEPENDS_ON.value,
                                       {"kind": "containerized_as"}, [prov]))

        # Base image as external dependency
        if base_image:
            img_id = make_node_id(NodeType.EXTERNAL_DEPENDENCY.value, base_image, namespace)
            result.nodes.append(KGNode(
                id=img_id, type=NodeType.EXTERNAL_DEPENDENCY.value, name=base_image,
                canonical_name=base_image, namespace=namespace,
                attributes={"kind": "docker_image"},
                provenance=[prov],
            ))
            result.edges.append(KGEdge(node_id, img_id, RelationType.DEPENDS_ON.value,
                                       {"kind": "base_image"}, [prov]))

    def _parse_compose(self, result: ParseResult, rel_path: str, content: str,
                       owner_service: Optional[str], namespace: str, prov: Provenance):
        """Parse docker-compose.yaml services."""
        try:
            data = yaml.safe_load(content)
        except Exception as exc:
            result.warnings.append(f"compose parse failed: {rel_path}: {exc}")
            return

        if not isinstance(data, dict):
            return

        services = data.get("services", {})
        if not isinstance(services, dict):
            return

        for svc_name, svc_config in services.items():
            if not isinstance(svc_config, dict):
                continue

            image = svc_config.get("image", "")
            ports = svc_config.get("ports", [])
            env = svc_config.get("environment", {})
            depends = svc_config.get("depends_on", [])

            node_id = make_node_id("Container", svc_name, namespace)
            result.nodes.append(KGNode(
                id=node_id, type="Container", name=svc_name,
                canonical_name=svc_name, namespace=namespace,
                attributes={
                    "image": image, "ports": ports,
                    "env_count": len(env) if isinstance(env, (dict, list)) else 0,
                    "source_file": rel_path,
                },
                provenance=[prov],
            ))

            # depends_on edges
            if isinstance(depends, list):
                for dep in depends:
                    dep_id = make_node_id("Container", dep, namespace)
                    result.edges.append(KGEdge(node_id, dep_id, RelationType.DEPENDS_ON.value,
                                               {"kind": "compose_depends_on"}, [prov]))
            elif isinstance(depends, dict):
                for dep in depends:
                    dep_id = make_node_id("Container", dep, namespace)
                    result.edges.append(KGEdge(node_id, dep_id, RelationType.DEPENDS_ON.value,
                                               {"kind": "compose_depends_on"}, [prov]))
