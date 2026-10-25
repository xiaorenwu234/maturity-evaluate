"""API specification parser.

Handles OpenAPI 3.x and Swagger 2.0 specs (YAML/JSON).
Extracts endpoints, operations, and schemas as API nodes.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import yaml

from ....graph.model import KGEdge, KGNode, Provenance, make_node_id
from ....graph.ontology import NodeType, RelationType, SourceType
from .base import BaseConfigParser, ParseResult

logger = logging.getLogger(__name__)


class ApiSpecParser(BaseConfigParser):
    category = "api_spec"

    @staticmethod
    def can_parse(rel_path: str, ext: str, content: str) -> bool:
        name = rel_path.rsplit("/", 1)[-1].lower()
        if any(k in name for k in ("openapi", "swagger")):
            return ext in (".yaml", ".yml", ".json")
        # Content-based detection
        head = content[:500]
        if '"openapi"' in head or "'openapi'" in head or "openapi:" in head:
            return True
        if '"swagger"' in head or "'swagger'" in head or "swagger:" in head:
            return True
        return False

    def parse(self, rel_path: str, content: str, owner_service: Optional[str],
              namespace: str) -> ParseResult:
        result = ParseResult()
        prov = Provenance(
            source_type=SourceType.DOCUMENT,
            source_id=f"doc_parser:api_spec:{rel_path}",
            evidence=f"API spec from {rel_path}",
            confidence=0.9,
            detail={"parser": "api_spec", "file": rel_path},
        )

        data = self._load(content, rel_path, result)
        if not data:
            return result

        info = data.get("info", {}) or {}
        title = info.get("title", rel_path)
        version = info.get("version", "")

        paths = data.get("paths", {}) or {}
        endpoint_count = 0

        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, spec in methods.items():
                if method.upper() not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
                    continue
                endpoint_count += 1
                op_id = spec.get("operationId", f"{method}_{path}") if isinstance(spec, dict) else f"{method}_{path}"
                summary = spec.get("summary", "") if isinstance(spec, dict) else ""

                api_id = make_node_id(NodeType.API.value, f"{method.upper()} {path}", namespace)
                result.nodes.append(KGNode(
                    id=api_id, type=NodeType.API.value, name=f"{method.upper()} {path}",
                    canonical_name=f"{method.upper()} {path}", namespace=namespace,
                    attributes={
                        "method": method.upper(), "path": path,
                        "operation_id": op_id, "summary": summary,
                        "source_file": rel_path,
                    },
                    provenance=[prov],
                ))

                # Link to owner service via EXPOSES
                if owner_service:
                    svc_id = make_node_id(NodeType.SERVICE.value, owner_service, namespace)
                    result.edges.append(KGEdge(svc_id, api_id, RelationType.EXPOSES.value,
                                               {"spec_file": rel_path}, [prov]))

        # Top-level API node summarizing the spec
        spec_id = make_node_id(NodeType.API.value, f"spec/{title}", namespace)
        result.nodes.append(KGNode(
            id=spec_id, type=NodeType.API.value, name=title,
            canonical_name=f"spec/{title}", namespace=namespace,
            attributes={
                "version": version, "endpoint_count": endpoint_count,
                "source_file": rel_path,
            },
            provenance=[prov],
        ))

        return result

    @staticmethod
    def _load(content: str, rel_path: str, result: ParseResult) -> Optional[dict]:
        try:
            if rel_path.endswith(".json"):
                return json.loads(content)
            else:
                return yaml.safe_load(content)
        except Exception as exc:
            result.warnings.append(f"api_spec parse failed: {rel_path}: {exc}")
            return None
