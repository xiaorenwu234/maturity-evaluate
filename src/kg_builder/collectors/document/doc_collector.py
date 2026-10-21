"""Document collector: architecture / interface / configuration docs -> graph.

Two families of inputs are handled:

* Prose docs (``.md`` / ``.rst`` / ``.txt``): become :class:`NodeType.DOCUMENT`
  nodes; service names (``\\w+Service`` …) and HTTP paths mentioned in the text
  are surfaced so they can merge with, or stand alongside, code-derived facts.
* Structured config files (``.yaml`` / ``.json`` / ``.toml`` / ``.ini`` /
  ``.properties``): flattened into :class:`NodeType.CONFIG` nodes, linked to a
  Service via ``CONFIGURED_BY`` when a service name is recognisable in the file.

Everything is tagged with ``source=document`` provenance -- a config value that
only *docs* claim (but code/deployment disagree with) is exactly the kind of
cross-source discrepancy Module 2 later reasons about.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ...config import BuildSettings
from ...graph.model import KGEdge, KGNode, Provenance, make_node_id
from ...graph.ontology import NodeType, RelationType, SourceType
from ..base import BaseCollector, CollectorResult

_SERVICE_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9]+(?:Service|Controller|Gateway|Client|Manager))\b")
_API_PATH_RE = re.compile(r"(?<![\w/])(/(?:[a-zA-Z0-9_\-]+/)*[a-zA-Z0-9_\-]+)\b")
_CONFIG_EXTS = {".yaml", ".yml", ".json", ".toml", ".ini", ".properties"}
_TEXT_EXTS = {".md", ".markdown", ".rst", ".txt"}

# Names that are too generic to be treated as a real service/config.
_STOP_CONFIG_KEYS = {"version", "name", "enabled", "type", "kind", "metadata", "spec"}


class DocumentCollector(BaseCollector):
    name = "document"

    def __init__(self, settings: BuildSettings) -> None:
        self.settings = settings
        self.doc = settings.document
        self.root = Path(settings.docs_path or settings.repo_path)
        self.namespace = settings.namespace

    def is_enabled(self) -> bool:
        return self.doc.enabled and self.root.exists()

    def collect(self) -> CollectorResult:
        result = CollectorResult(collector=self.name)
        scanned = 0
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in set(self.settings.code.exclude_dirs) for part in path.parts):
                continue
            ext = path.suffix.lower()
            if ext not in (_CONFIG_EXTS | _TEXT_EXTS):
                continue
            try:
                if path.stat().st_size > self.doc.max_file_bytes:
                    result.warnings.append(f"skipped (too large): {path}")
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError) as exc:
                result.warnings.append(f"read failed: {path}: {exc}")
                continue
            rel = self._rel(path)
            if ext in _CONFIG_EXTS:
                self._handle_config(result, path, rel, ext, text)
            else:
                self._handle_text(result, path, rel, text)
            scanned += 1
        result.metrics["documents_scanned"] = scanned
        return result

    # ------------------------------------------------------------- text docs
    def _handle_text(self, result: CollectorResult, path: Path, rel: str, text: str) -> None:
        doc_node = KGNode(
            id=make_node_id(NodeType.DOCUMENT.value, rel, self.namespace),
            type=NodeType.DOCUMENT.value,
            name=path.name,
            canonical_name=rel,
            namespace=self.namespace,
            attributes={"format": path.suffix.lstrip("."), "kind": "prose", "path": rel},
            provenance=[self._prov(rel, 1, f"document: {rel}")],
        )
        result.add_node(doc_node)

        for svc in set(_SERVICE_TOKEN_RE.findall(text)):
            svc_node = KGNode(
                id=make_node_id(NodeType.SERVICE.value, svc, self.namespace),
                type=NodeType.SERVICE.value,
                name=svc,
                canonical_name=svc,
                namespace=self.namespace,
                attributes={"mentioned_in": rel},
                provenance=[self._prov(rel, self._line_of(text, svc), f"mentions {svc}")],
            )
            result.add_node(svc_node)
            result.add_edge(KGEdge(doc_node.id, svc_node.id, RelationType.DEPENDS_ON.value,
                                   {"kind": "documents"}, [self._prov(rel, 1, f"{rel} documents {svc}")]))

        # API paths referenced in interface docs.
        for apipath in set(_API_PATH_RE.findall(text)):
            if apipath.count("/") < 1 or apipath in {"/", "//"}:
                continue
            label = f"ANY {apipath}"
            api_node = KGNode(
                id=make_node_id(NodeType.API.value, label.lower(), self.namespace),
                type=NodeType.API.value,
                name=label,
                canonical_name=label.lower(),
                namespace=self.namespace,
                attributes={"path": apipath, "from_doc": rel},
                provenance=[self._prov(rel, self._line_of(text, apipath), f"documents API {apipath}")],
            )
            result.add_node(api_node)

    # ----------------------------------------------------------- config docs
    def _handle_config(self, result: CollectorResult, path: Path, rel: str, ext: str, text: str) -> None:
        doc_node = KGNode(
            id=make_node_id(NodeType.DOCUMENT.value, rel, self.namespace),
            type=NodeType.DOCUMENT.value,
            name=path.name,
            canonical_name=rel,
            namespace=self.namespace,
            attributes={"format": ext.lstrip("."), "kind": "config", "path": rel},
            provenance=[self._prov(rel, 1, f"config document: {rel}")],
        )
        result.add_node(doc_node)

        owner_service = self._service_hint(rel, text)
        flat = self._flatten_config(ext, text, result, rel)
        for key, value in list(flat.items())[:500]:
            cfg_node = KGNode(
                id=make_node_id(NodeType.CONFIG.value, key.lower(), self.namespace),
                type=NodeType.CONFIG.value,
                name=key,
                canonical_name=key.lower(),
                namespace=self.namespace,
                attributes={"value": _short(value), "source_file": rel},
                provenance=[self._prov(rel, self._line_of(text, key.split('.')[-1]), f"{key}={_short(value)}")],
            )
            result.add_node(cfg_node)
            if owner_service:
                svc = KGNode(
                    id=make_node_id(NodeType.SERVICE.value, owner_service, self.namespace),
                    type=NodeType.SERVICE.value, name=owner_service, canonical_name=owner_service,
                    namespace=self.namespace, attributes={"configured_by_file": rel},
                    provenance=[self._prov(rel, 1, f"{owner_service} configured by {rel}")],
                )
                result.add_node(svc)
                result.add_edge(KGEdge(svc.id, cfg_node.id, RelationType.CONFIGURED_BY.value,
                                       {"declared_in": rel}, [self._prov(rel, 1, f"{owner_service}.{key}")]))

    def _flatten_config(self, ext: str, text: str, result: CollectorResult, rel: str) -> dict[str, Any]:
        data: Any = None
        try:
            if ext == ".json":
                data = json.loads(text)
            elif ext in (".yaml", ".yml"):
                import yaml  # type: ignore

                data = yaml.safe_load(text)
            elif ext == ".toml":
                try:
                    import tomllib  # py3.11+
                except ModuleNotFoundError:
                    tomllib = None  # type: ignore
                if tomllib is not None:
                    data = tomllib.loads(text)
            elif ext in (".ini", ".properties"):
                data = dict(re.findall(r"^\s*([\w.\-]+)\s*[=:]\s*(.+)$", text, re.M))
        except Exception as exc:  # noqa: BLE001 - tolerate malformed config
            result.warnings.append(f"config parse failed: {rel}: {exc}")
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
                    walk(v, f"{prefix}[{i}]") if isinstance(v, (dict, list)) else out.setdefault(f"{prefix}[{i}]", v)
            else:
                out[prefix] = node

        walk(data, "")
        return {k: v for k, v in out.items() if k.split(".")[-1].lower() not in _STOP_CONFIG_KEYS}

    # --------------------------------------------------------------- helpers
    def _service_hint(self, rel: str, text: str) -> Optional[str]:
        stem = Path(rel).name.split(".")[0]
        m = _SERVICE_TOKEN_RE.search(stem)
        if m:
            return m.group(1)
        found = _SERVICE_TOKEN_RE.findall(text)
        return found[0] if found else None

    @staticmethod
    def _line_of(text: str, needle: str) -> int:
        idx = text.find(needle)
        return text[:idx].count("\n") + 1 if idx >= 0 else 1

    def _prov(self, rel: str, line: int, evidence: str) -> Provenance:
        return Provenance(
            source_type=SourceType.DOCUMENT,
            source_id=f"{rel}:{line}",
            evidence=evidence,
            confidence=0.8,
            detail={"collector": self.name},
        )

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)


def _short(value: Any, limit: int = 120) -> str:
    s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"
