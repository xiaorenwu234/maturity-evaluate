"""Application configuration parser.

Handles Spring Boot application.yaml, Django settings, Flask config,
.env files, and generic TOML/INI/properties configs.
Enhanced version of the original _flatten_config with service detection.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import yaml

from ....graph.model import KGEdge, KGNode, Provenance, make_node_id
from ....graph.ontology import NodeType, RelationType, SourceType
from .base import BaseConfigParser, ParseResult

logger = logging.getLogger(__name__)

_STOP_KEYS = {"version", "name", "enabled", "type", "kind", "metadata", "spec"}

# Patterns that indicate external dependencies in config values
_DB_PATTERN = re.compile(r"(?:mysql|postgres|mongodb|redis|mariadb)://", re.I)
_MQ_PATTERN = re.compile(r"(?:amqp|rabbitmq|kafka|sqs)://", re.I)
_HTTP_PATTERN = re.compile(r"https?://[^\s\"']+", re.I)


class AppConfigParser(BaseConfigParser):
    category = "app_config"

    @staticmethod
    def can_parse(rel_path: str, ext: str, content: str) -> bool:
        name = rel_path.rsplit("/", 1)[-1].lower()
        # Spring Boot
        if name.startswith("application") and ext in (".yaml", ".yml", ".properties"):
            return True
        # Django / Flask
        if name in ("settings.py", "config.py", "config.json", "config.toml"):
            return True
        # .env files
        if name.startswith(".env") or name == "env":
            return True
        # Generic config files (catch-all for remaining structured configs)
        if ext in (".yaml", ".yml", ".json", ".toml", ".ini", ".properties"):
            # Only if not already claimed by a more specific parser
            return True
        return False

    def parse(self, rel_path: str, content: str, owner_service: Optional[str],
              namespace: str) -> ParseResult:
        result = ParseResult()
        prov = Provenance(
            source_type=SourceType.DOCUMENT,
            source_id=f"doc_parser:app_config:{rel_path}",
            evidence=f"App config from {rel_path}",
            confidence=0.85,
            detail={"parser": "app_config", "file": rel_path},
        )

        flat = self._flatten(rel_path, content, result)
        for key, value in list(flat.items())[:500]:
            cfg_id = make_node_id(NodeType.CONFIG.value, key.lower(), namespace)
            attrs: dict[str, Any] = {"value": str(value)[:120], "source_file": rel_path}

            # Detect external dependencies in values
            val_str = str(value)
            if _DB_PATTERN.search(val_str):
                attrs["external_kind"] = "database"
            elif _MQ_PATTERN.search(val_str):
                attrs["external_kind"] = "message_queue"
            elif _HTTP_PATTERN.search(val_str) and "localhost" not in val_str:
                attrs["external_kind"] = "external_service"

            result.nodes.append(KGNode(
                id=cfg_id, type=NodeType.CONFIG.value, name=key,
                canonical_name=key.lower(), namespace=namespace,
                attributes=attrs, provenance=[prov],
            ))

            if owner_service:
                svc_id = make_node_id(NodeType.SERVICE.value, owner_service, namespace)
                result.edges.append(KGEdge(svc_id, cfg_id, RelationType.CONFIGURED_BY.value,
                                           {"declared_in": rel_path}, [prov]))

                # External dependency edges
                ext_kind = attrs.get("external_kind")
                if ext_kind:
                    ext_id = make_node_id(NodeType.EXTERNAL_DEPENDENCY.value,
                                          f"{ext_kind}/{key.lower()}", namespace)
                    result.nodes.append(KGNode(
                        id=ext_id, type=NodeType.EXTERNAL_DEPENDENCY.value,
                        name=f"{ext_kind}:{key}", canonical_name=f"{ext_kind}/{key.lower()}",
                        namespace=namespace, attributes={"kind": ext_kind, "config_key": key},
                        provenance=[prov],
                    ))
                    result.edges.append(KGEdge(svc_id, ext_id, RelationType.DEPENDS_ON.value,
                                               {"kind": ext_kind, "via_config": rel_path}, [prov]))

        return result

    def _flatten(self, rel_path: str, content: str, result: ParseResult) -> dict[str, Any]:
        """Flatten structured config into key-value pairs."""
        ext = "." + rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""
        data: Any = None

        try:
            if ext == ".json":
                data = json.loads(content)
            elif ext in (".yaml", ".yml"):
                docs = list(yaml.safe_load_all(content))
                data = {}
                for doc in docs:
                    if isinstance(doc, dict):
                        data.update(doc)
            elif ext == ".toml":
                try:
                    import tomllib
                except ModuleNotFoundError:
                    tomllib = None
                if tomllib:
                    data = tomllib.loads(content)
            elif ext in (".ini", ".properties"):
                data = dict(re.findall(r"^\s*([\w.\-]+)\s*[=:]\s*(.+)$", content, re.M))
            elif rel_path.rsplit("/", 1)[-1].startswith(".env"):
                data = {}
                for line in content.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        data[k.strip()] = v.strip().strip("'\"")
        except Exception as exc:
            result.warnings.append(f"app_config parse failed: {rel_path}: {exc}")
            return {}

        out: dict[str, Any] = {}

        def walk(node: Any, prefix: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    key = f"{prefix}.{k}" if prefix else str(k)
                    if isinstance(v, (dict, list)):
                        walk(v, key)
                    else:
                        out[key] = v
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    if isinstance(v, (dict, list)):
                        walk(v, f"{prefix}[{i}]")
                    else:
                        out.setdefault(f"{prefix}[{i}]", v)
            else:
                out[prefix] = node

        if isinstance(data, dict):
            walk(data, "")
        return {k: v for k, v in out.items() if k.split(".")[-1].lower() not in _STOP_KEYS}
