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
import logging
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

logger = logging.getLogger(__name__)


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
        text_docs: list[tuple[str, str]] = []  # (rel_path, content) for LLM analysis

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
                text_docs.append((rel, text))
            scanned += 1

        # LLM-based document analysis for cross-source mapping
        if text_docs and self.settings.enrich:
            self._llm_analyze_docs(result, text_docs)

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

        # --- Layer 1: LLM analysis for service ownership & semantic summary ---
        llm_info = self._llm_analyze_config(rel, ext, text) if self.settings.enrich else None

        # Determine owner service: LLM result > heuristic fallback
        owner_service = None
        if llm_info and llm_info.get("owner_service"):
            owner_service = llm_info["owner_service"]
        else:
            owner_service = self._service_hint(rel, text)

        # --- Layer 2: Rule-based parsing for structured formats ---
        flat = self._flatten_config(ext, text, result, rel)

        if flat:
            # Structured parse succeeded: create Config nodes from flattened keys
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
        elif llm_info and llm_info.get("config_items"):
            # --- Layer 3: LLM-extracted config items when rule parsing fails ---
            logger.info(f"LLM extracted {len(llm_info['config_items'])} config items from {rel}")
            for item in llm_info["config_items"]:
                key = item.get("key", "")
                value = item.get("value", "")
                description = item.get("description", "")
                if not key:
                    continue
                cfg_prov = Provenance(
                    source_type=SourceType.DOCUMENT,
                    source_id=f"llm_config:{rel}",
                    evidence=f"LLM: {key}={_short(value)} ({description})",
                    confidence=0.7,
                    detail={"collector": self.name, "method": "llm"},
                )
                cfg_node = KGNode(
                    id=make_node_id(NodeType.CONFIG.value, key.lower(), self.namespace),
                    type=NodeType.CONFIG.value,
                    name=key,
                    canonical_name=key.lower(),
                    namespace=self.namespace,
                    attributes={"value": _short(value), "source_file": rel, "description": description},
                    provenance=[cfg_prov],
                )
                result.add_node(cfg_node)
                if owner_service:
                    svc = KGNode(
                        id=make_node_id(NodeType.SERVICE.value, owner_service, self.namespace),
                        type=NodeType.SERVICE.value, name=owner_service, canonical_name=owner_service,
                        namespace=self.namespace, attributes={"configured_by_file": rel},
                        provenance=[cfg_prov],
                    )
                    result.add_node(svc)
                    result.add_edge(KGEdge(svc.id, cfg_node.id, RelationType.CONFIGURED_BY.value,
                                           {"declared_in": rel, "source": "llm"}, [cfg_prov]))

        # Attach LLM semantic summary to the document node if available
        if llm_info and llm_info.get("summary"):
            doc_node.attributes["llm_summary"] = llm_info["summary"]

    def _flatten_config(self, ext: str, text: str, result: CollectorResult, rel: str) -> dict[str, Any]:
        data: Any = None
        try:
            if ext == ".json":
                data = json.loads(text)
            elif ext in (".yaml", ".yml"):
                import yaml  # type: ignore

                docs = list(yaml.safe_load_all(text))
                # Merge multiple YAML documents into one dict
                data = {}
                for doc in docs:
                    if isinstance(doc, dict):
                        data.update(doc)
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

    # --------------------------------------------------------- LLM analysis
    def _llm_analyze_config(self, rel: str, ext: str, text: str) -> Optional[dict]:
        """Use LLM to analyze a single config file.

        Returns a dict with:
        - owner_service: str (which service this config belongs to)
        - summary: str (semantic summary of what this config controls)
        - config_items: list[{key, value, description}] (for unparseable files)

        Returns None if LLM is unavailable or analysis fails.
        """
        try:
            from ...enrichment.llm_client import create_llm_client
            llm = create_llm_client(self.settings.llm)
        except Exception:
            return None

        # Truncate very large configs to avoid token limits
        truncated = text[:8000] if len(text) > 8000 else text

        system_prompt = (
            "你是资深软件架构分析师。分析以下配置文件，判断它属于哪个服务、控制什么行为。\n"
            "严格按 JSON 格式输出，不要添加解释文字。\n"
            '输出格式：{"owner_service": "ServiceName或null", '
            '"summary": "配置语义摘要", '
            '"config_items": [{"key": "...", "value": "...", "description": "..."}]}\n'
            "如果文件是标准结构化格式(YAML/JSON/TOML)，config_items可以为空数组（将由规则解析处理）。\n"
            "如果文件无法被规则解析，请提取关键配置项填入config_items。"
        )

        user_prompt = f"文件名: {rel}\n格式: {ext}\n\n内容:\n{truncated}"

        try:
            response = llm.complete_json(system_prompt, user_prompt)
            if isinstance(response, dict):
                return response
        except Exception as exc:
            logger.warning(f"LLM config analysis failed for {rel}: {exc}")
        return None

    def _llm_analyze_docs(
        self, result: CollectorResult, text_docs: list[tuple[str, str]]
    ) -> None:
        """Use LLM to extract service-deployment-source mappings from docs.

        Produces DEPLOYED_AS edges linking Service nodes to Deployment nodes,
        enabling cross-source correlation between code and K8s data.
        """
        try:
            from ...enrichment.llm_client import create_llm_client
        except Exception as exc:
            result.warnings.append(f"LLM client import failed: {exc}")
            return

        try:
            llm = create_llm_client(self.settings.llm)
        except Exception as exc:
            result.warnings.append(f"LLM client creation failed: {exc}")
            return

        # Concatenate all text docs into a single prompt
        docs_content = "\n\n---\n\n".join(
            f"## File: {rel}\n\n{content}" for rel, content in text_docs
        )

        system_prompt = (
            "你是资深软件架构分析师。分析以下项目文档，提取服务与部署的映射关系。\n"
            "严格按 JSON 格式输出，不要添加解释文字。\n"
            '输出格式：{"services": [{"service_name": "...", "k8s_deployment": "...", '
            '"k8s_namespace": "...", "source_files": ["..."], "responsibility": "..."}]}'
        )

        user_prompt = f"分析以下项目文档，提取所有服务的名称、对应的 K8s Deployment 名称、namespace、源码文件路径和职责描述。\n\n{docs_content}"

        try:
            response = llm.complete_json(system_prompt, user_prompt)
        except Exception as exc:
            result.warnings.append(f"LLM document analysis failed: {exc}")
            return

        services = response.get("services", []) if isinstance(response, dict) else []
        if not services:
            result.warnings.append("LLM returned no service mappings from documents")
            return

        logger.info(f"LLM extracted {len(services)} service mappings from documents")
        result.metrics["llm_service_mappings"] = len(services)

        for svc_info in services:
            svc_name = svc_info.get("service_name", "")
            k8s_deploy = svc_info.get("k8s_deployment", "")
            k8s_ns = svc_info.get("k8s_namespace", "")
            source_files = svc_info.get("source_files", [])
            responsibility = svc_info.get("responsibility", "")

            if not svc_name or not k8s_deploy:
                continue

            # Create/update Service node with LLM-derived attributes
            svc_id = make_node_id(NodeType.SERVICE.value, svc_name, self.namespace)
            prov = Provenance(
                source_type=SourceType.DOCUMENT,
                source_id="llm_doc_analysis",
                evidence=f"LLM: {svc_name} -> {k8s_deploy}",
                confidence=0.7,
                detail={"collector": self.name, "method": "llm"},
            )
            svc_node = KGNode(
                id=svc_id, type=NodeType.SERVICE.value, name=svc_name,
                canonical_name=svc_name, namespace=self.namespace,
                attributes={
                    "responsibility": responsibility,
                    "source_files": source_files,
                    "k8s_deployment": k8s_deploy,
                    "k8s_namespace": k8s_ns,
                },
                provenance=[prov],
            )
            result.add_node(svc_node)

            # Create DEPLOYED_AS edge: Service -> Deployment
            deploy_canon = f"{k8s_ns}/{k8s_deploy}" if k8s_ns else k8s_deploy
            deploy_id = make_node_id(NodeType.DEPLOYMENT.value, deploy_canon, self.namespace)
            deploy_prov = Provenance(
                source_type=SourceType.DOCUMENT,
                source_id="llm_doc_analysis",
                evidence=f"LLM: {svc_name} deployed as {k8s_deploy} in {k8s_ns}",
                confidence=0.7,
                detail={"collector": self.name, "method": "llm"},
            )
            result.add_edge(KGEdge(
                svc_id, deploy_id, RelationType.DEPLOYED_AS.value,
                {"k8s_namespace": k8s_ns, "source": "document_llm"},
                [deploy_prov],
            ))

            # Link source files to the service
            for src_file in source_files:
                file_id = make_node_id(NodeType.MODULE.value, src_file, self.namespace)
                file_prov = Provenance(
                    source_type=SourceType.DOCUMENT,
                    source_id="llm_doc_analysis",
                    evidence=f"LLM: {src_file} implements {svc_name}",
                    confidence=0.7,
                    detail={"collector": self.name, "method": "llm"},
                )
                result.add_edge(KGEdge(
                    svc_id, file_id, RelationType.IMPLEMENTS.value,
                    {"source": "document_llm"},
                    [file_prov],
                ))


def _short(value: Any, limit: int = 120) -> str:
    s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"
