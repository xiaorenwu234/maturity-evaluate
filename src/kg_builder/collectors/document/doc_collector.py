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

        # Use LLM to extract structured info from prose documents
        if self.settings.enrich:
            llm_info = self._llm_analyze_text(rel, text)
            if llm_info:
                self._apply_llm_text_result(result, doc_node, rel, llm_info)
            else:
                # LLM failed: fall back to heuristic extraction
                self._heuristic_text_extract(result, doc_node, rel, text)
        else:
            # enrich disabled: use heuristic only
            self._heuristic_text_extract(result, doc_node, rel, text)

    def _heuristic_text_extract(self, result: CollectorResult, doc_node: KGNode,
                                rel: str, text: str) -> None:
        """Fallback heuristic extraction when LLM is unavailable."""
        for svc in set(_SERVICE_TOKEN_RE.findall(text)):
            svc_node = KGNode(
                id=make_node_id(NodeType.SERVICE.value, svc, self.namespace),
                type=NodeType.SERVICE.value, name=svc, canonical_name=svc,
                namespace=self.namespace, attributes={"mentioned_in": rel},
                provenance=[self._prov(rel, self._line_of(text, svc), f"mentions {svc}")],
            )
            result.add_node(svc_node)
            result.add_edge(KGEdge(doc_node.id, svc_node.id, RelationType.DEPENDS_ON.value,
                                   {"kind": "documents"}, [self._prov(rel, 1, f"{rel} documents {svc}")]))

        for apipath in set(_API_PATH_RE.findall(text)):
            if apipath.count("/") < 1 or apipath in {"/", "//"}:
                continue
            label = f"ANY {apipath}"
            api_node = KGNode(
                id=make_node_id(NodeType.API.value, label.lower(), self.namespace),
                type=NodeType.API.value, name=label, canonical_name=label.lower(),
                namespace=self.namespace, attributes={"path": apipath, "from_doc": rel},
                provenance=[self._prov(rel, self._line_of(text, apipath), f"documents API {apipath}")],
            )
            result.add_node(api_node)

    def _llm_analyze_text(self, rel: str, text: str) -> Optional[dict]:
        """Use LLM to extract services, APIs, and relationships from a text document."""
        try:
            from ...enrichment.llm_client import create_llm_client
            llm = create_llm_client(self.settings.llm)
        except Exception:
            return None

        truncated = text[:6000] if len(text) > 6000 else text

        system_prompt = (
            "你是资深软件架构分析师。分析以下技术文档，提取其中提到的服务、API接口和调用关系。\n"
            "严格按 JSON 格式输出，不要添加解释文字。\n"
            '输出格式：{\n'
            '  "services": [{"name": "ServiceName", "responsibility": "职责描述"}],\n'
            '  "apis": [{"path": "/api/v1/orders", "method": "GET", "description": "描述"}],\n'
            '  "relationships": [{"from": "OrderService", "to": "InventoryService", "type": "CALLS", "description": "调用原因"}],\n'
            '  "summary": "文档内容摘要"\n'
            '}'
        )

        user_prompt = f"文档路径: {rel}\n\n内容:\n{truncated}"

        try:
            response = llm.complete_json(system_prompt, user_prompt)
            if isinstance(response, dict):
                return response
        except Exception as exc:
            logger.warning(f"LLM text analysis failed for {rel}: {exc}")
        return None

    def _apply_llm_text_result(self, result: CollectorResult, doc_node: KGNode,
                               rel: str, llm_info: dict) -> None:
        """Apply LLM-extracted structured info to the graph."""
        prov = Provenance(
            source_type=SourceType.DOCUMENT,
            source_id=f"llm_text:{rel}",
            evidence=f"LLM text analysis of {rel}",
            confidence=0.7,
            detail={"collector": self.name, "method": "llm"},
        )

        # Services mentioned in the document
        for svc_info in llm_info.get("services", []):
            svc_name = svc_info.get("name", "")
            if not svc_name:
                continue
            svc_id = make_node_id(NodeType.SERVICE.value, svc_name, self.namespace)
            result.add_node(KGNode(
                id=svc_id, type=NodeType.SERVICE.value, name=svc_name,
                canonical_name=svc_name, namespace=self.namespace,
                attributes={"mentioned_in": rel, "responsibility": svc_info.get("responsibility", "")},
                provenance=[prov],
            ))
            result.add_edge(KGEdge(doc_node.id, svc_id, RelationType.DEPENDS_ON.value,
                                   {"kind": "documents"}, [prov]))

        # API endpoints
        for api_info in llm_info.get("apis", []):
            path = api_info.get("path", "")
            method = api_info.get("method", "ANY")
            if not path:
                continue
            label = f"{method} {path}"
            api_id = make_node_id(NodeType.API.value, label.lower(), self.namespace)
            result.add_node(KGNode(
                id=api_id, type=NodeType.API.value, name=label,
                canonical_name=label.lower(), namespace=self.namespace,
                attributes={"path": path, "method": method, "from_doc": rel,
                            "description": api_info.get("description", "")},
                provenance=[prov],
            ))

        # Relationships between services
        for rel_info in llm_info.get("relationships", []):
            from_svc = rel_info.get("from", "")
            to_svc = rel_info.get("to", "")
            rel_type = rel_info.get("type", "CALLS")
            if not from_svc or not to_svc:
                continue
            from_id = make_node_id(NodeType.SERVICE.value, from_svc, self.namespace)
            to_id = make_node_id(NodeType.SERVICE.value, to_svc, self.namespace)
            # Map relationship type to ontology
            mapped_rel = RelationType.CALLS.value
            if rel_type.upper() == "DEPENDS_ON":
                mapped_rel = RelationType.DEPENDS_ON.value
            elif rel_type.upper() == "DEPLOYED_AS":
                mapped_rel = RelationType.DEPLOYED_AS.value
            result.add_edge(KGEdge(from_id, to_id, mapped_rel,
                                   {"source": "document_llm", "description": rel_info.get("description", "")},
                                   [prov]))

        # Attach summary to document node
        summary = llm_info.get("summary", "")
        if summary:
            doc_node.attributes["llm_summary"] = summary

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

        # --- LLM classification: category + owner_service + summary ---
        llm_info = self._llm_classify_config(rel, ext, text) if self.settings.enrich else None
        category = llm_info.get("category") if llm_info else None
        owner_service = None
        if llm_info and llm_info.get("owner_service"):
            owner_service = llm_info["owner_service"]
        else:
            owner_service = self._service_hint(rel, text)

        # Attach LLM semantic summary to the document node
        if llm_info and llm_info.get("summary"):
            doc_node.attributes["llm_summary"] = llm_info["summary"]

        # --- Find and invoke domain-specific parser ---
        from .utils import find_parser
        parser = find_parser(rel, ext, text, category)

        if parser is not None:
            logger.info(f"Using {parser.category} parser for {rel}")
            parse_result = parser.parse(rel, text, owner_service, self.namespace)
            for n in parse_result.nodes:
                result.add_node(n)
            for e in parse_result.edges:
                result.add_edge(e)
            result.warnings.extend(parse_result.warnings)
            result.metrics[f"parser_{parser.category}"] = len(parse_result.nodes)
        else:
            # No parser matched: fall back to generic flatten
            flat = self._flatten_config(ext, text, result, rel)
            for key, value in list(flat.items())[:500]:
                cfg_node = KGNode(
                    id=make_node_id(NodeType.CONFIG.value, key.lower(), self.namespace),
                    type=NodeType.CONFIG.value, name=key, canonical_name=key.lower(),
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
    def _llm_classify_config(self, rel: str, ext: str, text: str) -> Optional[dict]:
        """Use LLM to classify a config file and determine service ownership.

        Returns a dict with:
        - category: str (k8s/docker/monitor/ci/app_config/api_spec/iac)
        - owner_service: str or None
        - summary: str

        Returns None if LLM is unavailable or classification fails.
        """
        try:
            from ...enrichment.llm_client import create_llm_client
            llm = create_llm_client(self.settings.llm)
        except Exception:
            return None

        truncated = text[:4000] if len(text) > 4000 else text

        system_prompt = (
            "你是资深软件架构分析师。对以下配置文件进行分类和服务归属判断。\n"
            "严格按 JSON 格式输出，不要添加解释文字。\n"
            '分类选项(category): k8s, docker, monitor, ci, app_config, api_spec, iac, other\n'
            '输出格式：{"category": "...", "owner_service": "ServiceName或null", '
            '"summary": "一句话描述这个配置文件的用途"}'
        )

        user_prompt = f"文件名: {rel}\n格式: {ext}\n\n内容前4000字符:\n{truncated}"

        try:
            response = llm.complete_json(system_prompt, user_prompt)
            if isinstance(response, dict):
                return response
        except Exception as exc:
            logger.warning(f"LLM config classification failed for {rel}: {exc}")
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
