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
from ...enrichment.llm_client import call_structured
from ...graph.model import KGEdge, KGNode, Provenance, make_node_id
from ...graph.ontology import NodeType, RelationType, SourceType
from ..base import BaseCollector, CollectorResult, module_name_of
from .schemas import (
    BatchConfigClassification,
    BatchTextExtraction,
    ConfigClassification,
    ServiceMappings,
    TextExtraction,
    pick_by_path,
)

_SERVICE_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9]+(?:Service|Controller|Gateway|Client|Manager))\b")
# A path may carry template segments (``{id}`` / ``:id``) or dots; keep the tail
# tight so prose/backticks do not bleed into the capture.
_API_PATH_CHARS = r"[A-Za-z0-9_\-{}/:.]"
_API_PATH_RE = re.compile(rf"(?<![\w/])(/{_API_PATH_CHARS}+)")
# ``METHOD /path`` -- the verb lets the doc-derived API node share the exact
# canonical ("post /order/create") with the code-derived one and merge instead
# of spawning an "ANY /path" duplicate.
_API_MENTION_RE = re.compile(
    rf"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(/{_API_PATH_CHARS}+)", re.I
)
_CONFIG_EXTS = {".yaml", ".yml", ".json", ".toml", ".ini", ".properties"}
_TEXT_EXTS = {".md", ".markdown", ".rst", ".txt"}

# Names that are too generic to be treated as a real service/config.
_STOP_CONFIG_KEYS = {"version", "name", "enabled", "type", "kind", "metadata", "spec"}

# LLM batching / prompt budgets. One call now covers several files, which keeps
# the number of requests proportional to files/batch instead of files.
_LLM_BATCH_SIZE = 8
_LLM_BATCH_CHARS = 12_000
_LLM_DOC_CHARS = 6_000
_LLM_TOTAL_DOCS_CHARS = 24_000

_TEXT_EXTRACT_SYSTEM = (
    "你是资深软件架构分析师。分析以下技术文档，提取其中提到的服务、API接口和调用关系。\n"
    "严格按 JSON 格式输出，不要添加解释文字。\n"
    '输出格式：{\n'
    '  "services": [{"name": "ServiceName", "responsibility": "职责描述"}],\n'
    '  "apis": [{"path": "/api/v1/orders", "method": "GET", "description": "描述"}],\n'
    '  "relationships": [{"from": "OrderService", "to": "InventoryService", "type": "CALLS", "description": "调用原因"}],\n'
    '  "summary": "文档内容摘要"\n'
    '}'
)

_CONFIG_CLASSIFY_SYSTEM = (
    "你是资深软件架构分析师。对以下配置文件进行分类和服务归属判断。\n"
    "严格按 JSON 格式输出，不要添加解释文字。\n"
    '分类选项(category): k8s, docker, monitor, ci, app_config, api_spec, iac, other\n'
    '输出格式：{"category": "...", "owner_service": "ServiceName或null", '
    '"summary": "一句话描述这个配置文件的用途"}'
)

_BATCH_TEXT_EXTRACT_SYSTEM = (
    "你是资深软件架构分析师。你会收到多篇技术文档，只依据文档内容做抽取，不臆造实现。\n"
    "严格按 JSON 格式输出，不要添加解释文字。\n"
    '输出格式：{"documents": {"<文件路径>": {"services": [{"name": "...", '
    '"responsibility": "..."}], "apis": [{"path": "...", "method": "...", '
    '"description": "..."}], "relationships": [{"from": "...", "to": "...", '
    '"type": "CALLS", "description": "..."}], "summary": "..."}}}'
)

_BATCH_CONFIG_CLASSIFY_SYSTEM = (
    "你是资深软件架构分析师。你会收到多个配置文件，逐个分类并判断归属服务。\n"
    "严格按 JSON 格式输出，不要添加解释文字。\n"
    '输出格式：{"files": {"<文件路径>": {"category": "...", '
    '"owner_service": "ServiceName或null", "summary": "..."}}}'
    "category 取 k8s/docker/monitor/ci/app_config/api_spec/iac/other。"
)


def _llm_batches(items: list[tuple], size: int = _LLM_BATCH_SIZE,
                 budget: int = _LLM_BATCH_CHARS):
    """Yield item batches bounded by count and total text characters.

    Every item tuple stores its raw text as the last element, so the same
    helper serves prose docs ``(rel, text)`` and config files
    ``(rel, ext, text)``.
    """
    batch: list[tuple] = []
    used = 0
    for item in items:
        text_len = len(item[-1])
        if batch and (len(batch) >= size or used + text_len > budget):
            yield batch
            batch, used = [], 0
        batch.append(item)
        used += text_len
    if batch:
        yield batch

logger = logging.getLogger(__name__)


class DocumentCollector(BaseCollector):
    name = "document"

    def __init__(self, settings: BuildSettings) -> None:
        self.settings = settings
        self.doc = settings.document
        self.root = Path(settings.docs_path or settings.repo_path)
        self.namespace = settings.namespace
        # One LangChain client per collector run: building it per file used to
        # dominate the cost of scanning a docs-heavy repository.
        self._llm = None
        self._llm_error: Optional[BaseException] = None

    def _get_llm(self):
        if self._llm is None:
            if self._llm_error is not None:
                raise self._llm_error
            try:
                from ...enrichment.llm_client import create_llm_client
                self._llm = create_llm_client(self.settings.llm)
            except Exception as exc:
                # Cache the failure: retrying per file would rebuild the client
                # for every document in an unconfigured build.
                self._llm_error = exc
                raise
        return self._llm

    def is_enabled(self) -> bool:
        return self.doc.enabled and self.root.exists()

    def collect(self) -> CollectorResult:
        result = CollectorResult(collector=self.name)
        scanned = 0
        text_docs: list[tuple[str, str]] = []          # (rel, content)
        config_docs: list[tuple[Path, str, str, str]] = []  # (path, rel, ext, content)
        doc_nodes: dict[str, KGNode] = {}

        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in set(self.settings.code.exclude_dirs) for part in path.parts):
                continue
            if path.name in set(self.doc.exclude_files):
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
                doc_nodes[rel] = self._make_document_node(result, path, rel, ext, kind="config")
                config_docs.append((path, rel, ext, text))
            else:
                doc_nodes[rel] = self._make_document_node(result, path, rel, ext, kind="prose")
                text_docs.append((rel, text))
            scanned += 1

        # Batch the LLM work (one request per batch instead of one per file).
        text_info: dict[str, dict] = {}
        config_info: dict[str, dict] = {}
        text_llm_ok = True
        if self.settings.enrich:
            try:
                text_info = self._llm_analyze_texts(text_docs)
            except Exception as exc:  # noqa: BLE001 - degrade to heuristics + warning
                text_llm_ok = False
                result.warnings.append(f"LLM text analysis unavailable: {exc}")
            try:
                config_info = self._llm_classify_configs(
                    [(rel, ext, text) for _p, rel, ext, text in config_docs]
                )
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"LLM config classification unavailable: {exc}")

        for path, rel, ext, text in config_docs:
            self._apply_config_document(
                result, doc_nodes[rel], path, rel, ext, text, config_info.get(rel),
            )

        for rel, text in text_docs:
            info = text_info.get(rel)
            if not self.settings.enrich:
                self._heuristic_text_extract(result, doc_nodes[rel], rel, text)
            elif info:
                self._apply_llm_text_result(result, doc_nodes[rel], rel, info)
            else:
                if text_llm_ok:
                    result.warnings.append(
                        f"LLM text analysis returned no result for {rel}; using heuristic extraction"
                    )
                self._heuristic_text_extract(result, doc_nodes[rel], rel, text)

        # LLM-based document analysis for cross-source mapping (budgeted).
        if text_docs and self.settings.enrich:
            self._llm_analyze_docs(result, text_docs)

        # Deterministic manifest topology (no model required).
        self._link_k8s_workloads(result)

        result.metrics["documents_scanned"] = scanned
        return result

    def _make_document_node(self, result: CollectorResult, path: Path, rel: str,
                            ext: str, kind: str) -> KGNode:
        evidence = f"config document: {rel}" if kind == "config" else f"document: {rel}"
        node = KGNode(
            id=make_node_id(NodeType.DOCUMENT.value, rel, self.namespace),
            type=NodeType.DOCUMENT.value,
            name=path.name,
            canonical_name=rel,
            namespace=self.namespace,
            attributes={"format": ext.lstrip("."), "kind": kind, "path": rel},
            provenance=[self._prov(rel, 1, evidence)],
        )
        result.add_node(node)
        return node

    def _link_k8s_workloads(self, result: CollectorResult) -> None:
        """Link a manifest ``kind: Service`` to the workload its pods belong to.

        Manifests already state this via the service selector, so the relation
        needs no model. Without it a Service and the Deployment behind it stay
        two isolated nodes whenever no LLM classifies the file's owner service.
        """
        workloads: dict[tuple[str, str], KGNode] = {}
        for node in result.nodes:
            if node.type != NodeType.DEPLOYMENT.value:
                continue
            key = (str(node.attributes.get("k8s_namespace", "")), node.name)
            workloads.setdefault(key, node)
        if not workloads:
            return

        linked = 0
        for node in result.nodes:
            if node.type != NodeType.SERVICE.value or "service_type" not in node.attributes:
                continue
            ns = str(node.attributes.get("k8s_namespace", ""))
            selector = node.attributes.get("selector") or {}
            target = None
            if selector:
                for key, workload in workloads.items():
                    if key[0] != ns:
                        continue
                    pod_labels = workload.attributes.get("pod_labels") or {}
                    if all(pod_labels.get(k) == v for k, v in selector.items()):
                        target = workload
                        break
            if target is None:
                # No selector (or nothing matched): fall back to the same name.
                target = workloads.get((ns, node.name))
            if target is None:
                continue
            result.add_edge(KGEdge(
                node.id, target.id, RelationType.DEPENDS_ON.value,
                {"kind": "routes_to", "selector": selector},
                list(node.provenance[:1]),
            ))
            linked += 1
        result.metrics["k8s_service_links"] = linked

    # ------------------------------------------------------------- text docs
    def _handle_text(self, result: CollectorResult, path: Path, rel: str, text: str) -> None:
        """Single-file entry point (kept for callers/tests that process one file).

        ``collect()`` uses the batched path; this method stays as a thin wrapper
        so existing call sites keep working.
        """
        doc_node = self._make_document_node(
            result, path, rel, path.suffix.lower(), kind="prose",
        )

        # Use LLM to extract structured info from prose documents
        if self.settings.enrich:
            llm_info = self._llm_analyze_text(rel, text)
            if llm_info:
                self._apply_llm_text_result(result, doc_node, rel, llm_info)
            else:
                # Heuristic extraction is an explicit, documented mode (it also
                # backs enrich=False); record that the LLM path degraded so the
                # fallback is visible in build warnings rather than silent.
                result.warnings.append(
                    f"LLM text analysis failed for {rel}; using heuristic extraction"
                )
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
            result.add_edge(KGEdge(doc_node.id, svc_node.id, RelationType.MENTIONS.value,
                                   {"kind": "documents"}, [self._prov(rel, 1, f"{rel} documents {svc}")]))

        # Method-qualified endpoints first (carry the verb so they merge with the
        # code-derived API node); verb-less paths fall back to ANY and never
        # duplicate a path already captured with a concrete method.
        seen_paths: set[str] = set()
        doc_apis: list[tuple[str, str, str]] = []  # (label, path, method)
        for m in _API_MENTION_RE.finditer(text):
            apipath = m.group(2).rstrip("/") or m.group(2)
            method = m.group(1).upper()
            if apipath in seen_paths:
                continue
            seen_paths.add(apipath)
            doc_apis.append((f"{method} {apipath}", apipath, method))
        for apipath in sorted(set(_API_PATH_RE.findall(text))):
            apipath = apipath.rstrip("/") or apipath
            if apipath.count("/") < 1 or apipath in {"/", "//"} or apipath in seen_paths:
                continue
            seen_paths.add(apipath)
            doc_apis.append((f"ANY {apipath}", apipath, "ANY"))

        for label, apipath, method in doc_apis:
            api_node = KGNode(
                id=make_node_id(NodeType.API.value, label.lower(), self.namespace),
                type=NodeType.API.value, name=label, canonical_name=label.lower(),
                namespace=self.namespace,
                attributes={"path": apipath, "method": method, "from_doc": rel},
                provenance=[self._prov(rel, self._line_of(text, apipath), f"documents API {apipath}")],
            )
            result.add_node(api_node)
            result.add_edge(KGEdge(doc_node.id, api_node.id, RelationType.MENTIONS.value,
                                   {"kind": "documents"}, [self._prov(rel, 1, f"{rel} documents {apipath}")]))

    def _llm_analyze_texts(self, docs: list[tuple[str, str]]) -> dict[str, dict]:
        """Batch-extract services / APIs / relationships for several documents.

        Returns ``{rel: info}`` for the documents the model answered; missing
        entries fall back to heuristic extraction in :meth:`collect`.
        """
        if not docs:
            return {}
        llm = self._get_llm()
        out: dict[str, dict] = {}
        for batch in _llm_batches(docs):
            listing = "\n\n".join(
                f"=== FILE: {rel} ===\n{text[:_LLM_DOC_CHARS]}"
                for rel, text in batch
            )
            user = (
                "分析下列文档，逐个提取每篇文档提到的服务、API 接口和服务间关系。\n"
                "输出中的文件路径必须与输入完全一致。\n\n"
                f"{listing}"
            )
            parsed = call_structured(
                llm, _BATCH_TEXT_EXTRACT_SYSTEM, user, BatchTextExtraction
            )
            for rel, _text in batch:
                info = pick_by_path(parsed.documents, rel)
                if info is not None:
                    out[rel] = info.to_payload()
        return out

    def _llm_analyze_text(self, rel: str, text: str) -> Optional[dict]:
        """Use LLM to extract services, APIs, and relationships from a text document."""
        try:
            llm = self._get_llm()
        except Exception:
            return None

        truncated = text[:_LLM_DOC_CHARS]
        user_prompt = f"文档路径: {rel}\n\n内容:\n{truncated}"

        try:
            return call_structured(
                llm, _TEXT_EXTRACT_SYSTEM, user_prompt, TextExtraction
            ).to_payload()
        except Exception as exc:
            logger.warning(f"LLM text analysis failed for {rel}: {exc}")
        return None

    def _apply_llm_text_result(self, result: CollectorResult, doc_node: KGNode,
                               rel: str, llm_info: dict) -> None:
        """Apply LLM-extracted structured info to the graph."""
        prov = Provenance(
            source_type=SourceType.LLM,
            source_id=f"llm://document/{rel}",
            evidence=f"LLM text analysis of {rel}",
            confidence=0.7,
            detail={"collector": self.name, "method": "llm"},
        )

        # Services mentioned in the document
        seen_services: set[str] = set()
        for svc_info in llm_info.get("services", []):
            svc_name = svc_info.get("name", "")
            if not svc_name:
                continue
            svc_id = make_node_id(NodeType.SERVICE.value, svc_name, self.namespace)
            seen_services.add(svc_id)
            result.add_node(KGNode(
                id=svc_id, type=NodeType.SERVICE.value, name=svc_name,
                canonical_name=svc_name, namespace=self.namespace,
                attributes={"mentioned_in": rel, "responsibility": svc_info.get("responsibility", "")},
                provenance=[prov],
            ))
            result.add_edge(KGEdge(doc_node.id, svc_id, RelationType.MENTIONS.value,
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
            result.add_edge(KGEdge(doc_node.id, api_id, RelationType.MENTIONS.value,
                                   {"kind": "documents"}, [prov]))

        # Relationships between services
        for rel_info in llm_info.get("relationships", []):
            from_svc = rel_info.get("from", "")
            to_svc = rel_info.get("to", "")
            rel_type = rel_info.get("type", "CALLS")
            if not from_svc or not to_svc:
                continue
            from_id = make_node_id(NodeType.SERVICE.value, from_svc, self.namespace)
            to_id = make_node_id(NodeType.SERVICE.value, to_svc, self.namespace)
            # Endpoints must exist in this result, otherwise the pipeline drops
            # the edge as dangling -- so materialise any service the LLM did not
            # list under "services".
            for svc_name, svc_id in ((from_svc, from_id), (to_svc, to_id)):
                if svc_id in seen_services:
                    continue
                seen_services.add(svc_id)
                result.add_node(KGNode(
                    id=svc_id, type=NodeType.SERVICE.value, name=svc_name,
                    canonical_name=svc_name, namespace=self.namespace,
                    attributes={"mentioned_in": rel}, provenance=[prov],
                ))
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
        """Single-file entry point (kept for callers/tests that process one file).

        ``collect()`` classifies config files in batches; this wrapper keeps the
        original one-file-at-a-time behaviour available.
        """
        doc_node = self._make_document_node(result, path, rel, ext, kind="config")
        llm_info = self._llm_classify_config(rel, ext, text) if self.settings.enrich else None
        self._apply_config_document(result, doc_node, path, rel, ext, text, llm_info)

    def _apply_config_document(self, result: CollectorResult, doc_node: KGNode,
                               path: Path, rel: str, ext: str, text: str,
                               llm_info: Optional[dict]) -> None:
        """Dispatch one config file to its domain parser and emit graph facts."""
        if self.settings.enrich and llm_info is None:
            result.warnings.append(
                f"LLM config classification failed for {rel}; using filename/content heuristics"
            )
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
    def _llm_classify_configs(self, configs: list[tuple[str, str, str]]) -> dict[str, dict]:
        """Batch-classify config files: ``{rel: {category, owner_service, summary}}``.

        Missing entries fall back to filename/content heuristics in
        :meth:`_apply_config_document`.
        """
        if not configs:
            return {}
        llm = self._get_llm()
        out: dict[str, dict] = {}
        for batch in _llm_batches(configs):
            listing = "\n\n".join(
                f"=== FILE: {rel} (format={ext}) ===\n{text[:_LLM_DOC_CHARS]}"
                for rel, ext, text in batch
            )
            user = (
                "对下列配置文件逐个分类并判断归属服务。\n"
                "输出中的文件路径必须与输入完全一致。\n\n"
                f"{listing}"
            )
            parsed = call_structured(
                llm, _BATCH_CONFIG_CLASSIFY_SYSTEM, user, BatchConfigClassification
            )
            for rel, _ext, _text in batch:
                info = pick_by_path(parsed.files, rel)
                if info is not None:
                    out[rel] = info.to_payload()
        return out

    def _llm_classify_config(self, rel: str, ext: str, text: str) -> Optional[dict]:
        """Use LLM to classify a config file and determine service ownership.

        Returns a :class:`ConfigClassification` payload (``category``,
        ``owner_service``, ``summary`` -- every field present, because the schema
        fills what the model omits).

        Returns None if LLM is unavailable or classification fails.
        """
        try:
            llm = self._get_llm()
        except Exception:
            return None

        truncated = text[:_LLM_DOC_CHARS]
        user_prompt = f"文件名: {rel}\n格式: {ext}\n\n内容:\n{truncated}"

        try:
            return call_structured(
                llm, _CONFIG_CLASSIFY_SYSTEM, user_prompt, ConfigClassification
            ).to_payload()
        except Exception as exc:
            logger.warning(f"LLM config classification failed for {rel}: {exc}")
        return None

    @staticmethod
    def _docs_payload(result: CollectorResult,
                      text_docs: list[tuple[str, str]]) -> str:
        """Concatenate docs under a hard character budget.

        The global mapping prompt used to embed every document verbatim, which
        overflows the context window on docs-heavy repositories. Later documents
        are omitted (with a warning) once the budget is exhausted.
        """
        parts: list[str] = []
        used = 0
        omitted = 0
        separator = "\n\n---\n\n"
        for rel, content in text_docs:
            prefix = separator if parts else ""
            remaining = _LLM_TOTAL_DOCS_CHARS - used - len(prefix)
            if remaining <= 0:
                omitted += 1
                continue
            chunk = f"## File: {rel}\n\n{content}"
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            parts.append(prefix + chunk)
            used += len(prefix) + len(chunk)
        if omitted:
            result.warnings.append(
                f"LLM document analysis truncated: {omitted} document(s) omitted "
                f"after {_LLM_TOTAL_DOCS_CHARS} characters"
            )
        return "".join(parts)

    def _llm_analyze_docs(
        self, result: CollectorResult, text_docs: list[tuple[str, str]]
    ) -> None:
        """Use LLM to extract service-deployment-source mappings from docs.

        Produces DEPLOYED_AS edges linking Service nodes to Deployment nodes,
        enabling cross-source correlation between code and K8s data.
        """
        try:
            llm = self._get_llm()
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"LLM client unavailable: {exc}")
            return

        # Concatenate docs under a hard budget instead of sending everything.
        docs_content = self._docs_payload(result, text_docs)

        system_prompt = (
            "你是资深软件架构分析师。分析以下项目文档，提取服务与部署的映射关系。\n"
            "严格按 JSON 格式输出，不要添加解释文字。\n"
            '输出格式：{"services": [{"service_name": "...", "k8s_deployment": "...", '
            '"k8s_namespace": "...", "source_files": ["..."], "responsibility": "..."}]}'
        )

        user_prompt = f"分析以下项目文档，提取所有服务的名称、对应的 K8s Deployment 名称、namespace、源码文件路径和职责描述。\n\n{docs_content}"

        try:
            parsed = call_structured(llm, system_prompt, user_prompt, ServiceMappings)
        except Exception as exc:
            result.warnings.append(f"LLM document analysis failed: {exc}")
            return

        services = [mapping.to_payload() for mapping in parsed.services]
        if not services:
            result.warnings.append("LLM returned no service mappings from documents")
            return

        logger.info(f"LLM extracted {len(services)} service mappings from documents")
        result.metrics["llm_service_mappings"] = len(services)

        # Edges below reference Service / Deployment / Module endpoints. Emitting
        # those nodes here is what keeps the pipeline from dropping the edges as
        # dangling (add_edge requires both endpoints to exist). Existing nodes
        # (e.g. from the K8s manifest parser) are merged in place, mirroring
        # KnowledgeGraph.add_node semantics -- first writer wins on attributes.
        node_index: dict[str, KGNode] = {n.id: n for n in result.nodes}

        def ensure_node(node: KGNode) -> None:
            existing = node_index.get(node.id)
            if existing is None:
                node_index[node.id] = node
                result.add_node(node)
                return
            for k, v in node.attributes.items():
                existing.attributes.setdefault(k, v)
            for p in node.provenance:
                existing.add_provenance(p)

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
                source_type=SourceType.LLM,
                source_id="llm://document/service_mapping",
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
            ensure_node(svc_node)

            # Create DEPLOYED_AS edge: Service -> Deployment
            deploy_canon = f"{k8s_ns}/{k8s_deploy}" if k8s_ns else k8s_deploy
            deploy_id = make_node_id(NodeType.DEPLOYMENT.value, deploy_canon, self.namespace)
            deploy_prov = Provenance(
                source_type=SourceType.LLM,
                source_id="llm://document/service_mapping",
                evidence=f"LLM: {svc_name} deployed as {k8s_deploy} in {k8s_ns}",
                confidence=0.7,
                detail={"collector": self.name, "method": "llm"},
            )
            ensure_node(KGNode(
                id=deploy_id, type=NodeType.DEPLOYMENT.value, name=k8s_deploy,
                canonical_name=deploy_canon, namespace=self.namespace,
                attributes={"k8s_namespace": k8s_ns, "source": "document_llm"},
                provenance=[deploy_prov],
            ))
            result.add_edge(KGEdge(
                svc_id, deploy_id, RelationType.DEPLOYED_AS.value,
                {"k8s_namespace": k8s_ns, "source": "document_llm"},
                [deploy_prov],
            ))

            # Link source files to the service. The module key must match what
            # CodeCollector stores on Module nodes (dotted module / Java file
            # module), otherwise the IMPLEMENTS edge points at a phantom node.
            for src_file in source_files:
                module_key = module_name_of(src_file)
                file_id = make_node_id(NodeType.MODULE.value, module_key, self.namespace)
                file_prov = Provenance(
                    source_type=SourceType.LLM,
                    source_id="llm://document/service_mapping",
                    evidence=f"LLM: {src_file} implements {svc_name}",
                    confidence=0.7,
                    detail={"collector": self.name, "method": "llm"},
                )
                ensure_node(KGNode(
                    id=file_id, type=NodeType.MODULE.value, name=module_key,
                    canonical_name=module_key, namespace=self.namespace,
                    attributes={"path": src_file},
                    provenance=[file_prov],
                ))
                result.add_edge(KGEdge(
                    svc_id, file_id, RelationType.IMPLEMENTS.value,
                    {"source": "document_llm", "path": src_file},
                    [file_prov],
                ))


def _short(value: Any, limit: int = 120) -> str:
    s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"
