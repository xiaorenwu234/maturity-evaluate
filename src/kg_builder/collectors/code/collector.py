"""CodeCollector: open-source-toolkit-driven code analysis collector.

Replaces the legacy ``CodeCollector`` with precise call resolution via
scip-python, radon metrics and tree-sitter CFG/clone detection. The output
contract (``CollectorResult`` with nodes/edges/provenance/metrics/extras) is
identical so downstream pipeline fusion and evaluation require zero changes.

Hard-failure semantics: if any toolkit is unavailable, ``collect()`` raises
immediately rather than degrading. This matches the user-confirmed decision
that the module must be a complete replacement, not a layered fallback.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from ...config import BuildSettings
from ...graph.model import KGEdge, KGNode, Provenance, make_node_id
from ...graph.ontology import NodeType, RelationType, SourceType
from ..base import BaseCollector, CollectorResult, python_module_name
from .schema.artifact import CallRef, FileArtifact
from .enrichment.frameworks import enrich_all
from .python import status as toolkit_status
from .python.clone_detector import CLONE_MIN_TOKENS
from .python.ir_compat import build_artifact
from .python.scip_resolver import ScipIndex
from .python.scip_runner import ScipIndexError, index_repo


_SERVICE_SUFFIXES = ("Service", "Controller", "Router", "Resource", "Api", "API", "Handler", "Manager")
_CONFIDENCE = {"python": 0.95}
_CONF_HTTP_BRIDGE = 0.6


class CodeCollector(BaseCollector):
    name = "code"

    def __init__(self, settings: BuildSettings) -> None:
        self.settings = settings
        self.code = settings.code
        self.repo = Path(settings.repo_path)
        self.namespace = settings.namespace

    def is_enabled(self) -> bool:
        return self.code.enabled and self.repo.exists()

    # ------------------------------------------------------------------ run
    def collect(self) -> CollectorResult:
        result = CollectorResult(collector=self.name)

        # One pruned walk: excluded directories are never descended into, and
        # the same file list feeds both language detection and artifact
        # building (the previous raw rglob also tripped over node_modules).
        source_paths = self._iter_source_files()
        has_python = any(p.suffix == ".py" for p in source_paths)
        has_java = any(p.suffix == ".java" for p in source_paths)

        # Check only the toolkits needed for the languages present.
        statuses = toolkit_status.check_all()
        missing = []
        if has_python and not statuses.get("scip", (False,))[0]:
            missing.append(f"scip: {statuses.get('scip', ('', 'unknown'))[1]}")
        if has_python and not statuses.get("radon", (False,))[0]:
            missing.append(f"radon: {statuses.get('radon', ('', 'unknown'))[1]}")
        if has_python and not statuses.get("treesitter_python", (False,))[0]:
            missing.append(f"treesitter_python: {statuses.get('treesitter_python', ('', 'unknown'))[1]}")
        if has_java and self.settings.code_analysis.java_enabled and not statuses.get("joern", (False,))[0]:
            missing.append(f"joern: {statuses.get('joern', ('', 'unknown'))[1]}")
        if has_java and self.settings.code_analysis.java_enabled:
            ts_java_ok, ts_java_detail = toolkit_status.treesitter_java_status()
            if not ts_java_ok:
                missing.append(f"treesitter_java: {ts_java_detail}")
        if missing:
            details = "; ".join(missing)
            raise RuntimeError(
                f"code collector requires toolkits for detected languages; missing: {details}"
            )

        # Index the whole repo once via scip-python (Python files).
        scip_index = None
        if has_python:
            scip_path: Optional[Path] = None
            try:
                scip_path = index_repo(self.repo)
                from .python.scip_resolver import load_index
                scip_index = load_index(scip_path, self.repo)
            except ScipIndexError as exc:
                raise RuntimeError(f"code collector: SCIP indexing failed: {exc}") from exc
            finally:
                # scip_path stays unbound when index_repo() itself fails; guard
                # explicitly instead of relying on a blanket except.
                if scip_path is not None:
                    try:
                        scip_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        # Optionally index Java files via Joern.
        java_call_index = None
        if has_java and self.settings.code_analysis.java_enabled:
            try:
                from .java.joern_runner import build_cpg, export_flatgraph, joern_status
                from .java.cpg_parser import parse_dot_export
                ok, _ = joern_status()
                if ok:
                    cpg_path = build_cpg(self.repo)
                    dot_dir = export_flatgraph(cpg_path)
                    java_call_index = parse_dot_export(dot_dir)
            except Exception as exc:
                result.warnings.append(f"Joern indexing failed (Java calls will be unresolved): {exc}")

        # Walk source files and build artifacts.
        artifacts: list[FileArtifact] = []
        scanned = 0
        parse_failures = 0
        for p in source_paths:
            try:
                source = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                result.warnings.append(f"read failed: {p}: {exc}")
                continue
            if len(source.encode("utf-8")) > self.code.max_file_bytes:
                result.warnings.append(f"skipped (too large): {p}")
                continue
            rel = str(p.relative_to(self.repo))
            if p.suffix == ".py":
                module = self._module_of(rel)
                art = build_artifact(rel, source, module, scip_index)
            elif p.suffix == ".java":
                from .java.java_ir_adapter import build_java_artifact, _java_module_of
                from .java.cpg_parser import JavaCallIndex as _EmptyJavaIdx
                module = _java_module_of(rel)
                art = build_java_artifact(rel, source, module, java_call_index or _EmptyJavaIdx())
            else:
                continue
            artifacts.append(art)
            scanned += 1
            if not (art.classes or art.functions or art.imports) and source.strip():
                parse_failures += 1

        # Framework semantic enrichment (Semgrep channel, reused unchanged).
        fw_status = enrich_all(artifacts, self.repo, self.code.exclude_dirs)
        result.metrics.update(
            files_scanned=scanned,
            files_without_symbols=parse_failures,
            extractor={"python": "code (scip+radon+treesitter)"},
            semgrep=f"{fw_status['engine']} (findings={fw_status['findings']})",
            semgrep_applied=fw_status["applied"],
            toolkits={k: v[1] for k, v in statuses.items()},
        )
        result.warnings.extend(fw_status["warnings"])

        # Detect services and emit graph primitives.
        service_classes = self._detect_services(artifacts)
        self._emit(result, artifacts, service_classes)

        # CFG + clone extras (same shape as legacy collector).
        result.extras["cfg"] = {
            f.qualified_name: {"file": art.path, "cfg": f.cfg}
            for art in artifacts for f in art.functions if f.cfg
        }
        return result

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _module_of(rel: str) -> str:
        return python_module_name(rel)

    def _iter_source_files(self) -> list[Path]:
        """List analysable source files without descending into excluded dirs."""
        exclude = set(self.code.exclude_dirs)
        out: list[Path] = []
        for root, dirnames, filenames in os.walk(self.repo):
            dirnames[:] = sorted(d for d in dirnames if d not in exclude)
            for name in sorted(filenames):
                p = Path(root) / name
                if p.suffix in (".py", ".java"):
                    out.append(p)
        return out

    def _detect_services(self, artifacts: list[FileArtifact]) -> set[str]:
        services: set[str] = set()
        for art in artifacts:
            # 1. Semgrep-detected service classes (framework annotations).
            services.update(art.service_classes)
            # 2. Suffix-based detection as fallback for non-framework code.
            for c in art.classes:
                if any(c.name.endswith(s) for s in _SERVICE_SUFFIXES):
                    services.add(c.name)
            # 3. Classes with route decorators.
            handler_classes = {
                f.class_name for f in art.functions
                if f.class_name and self._has_route_decorator(f)
            }
            services |= handler_classes
        return {s for s in services if s}

    @staticmethod
    def _has_route_decorator(fn) -> bool:
        return any(
            re.search(r"\.(get|post|put|delete|patch|route)\b", d or "") for d in fn.decorators
        )

    # ---------------------------------------------------------------- emit
    def _emit(self, result, artifacts, services):
        node_index: dict[str, KGNode] = {}
        clone_groups: dict[str, list] = {}

        # Structural indices built in pass 1, consumed when wiring edges in pass 2.
        module_nodes: dict[str, KGNode] = {}                # art.path -> Module node
        classes_by_art: dict[str, dict[str, KGNode]] = {}   # art.path -> {class name -> Class node}
        functions_by_qname: dict[str, KGNode] = {}          # function qualified_name -> Function node
        class_by_qname: dict[str, KGNode] = {}              # class qualified_name -> Class node
        class_by_simple: dict[str, Optional[KGNode]] = {}   # class name -> node (None if ambiguous)
        service_class_pairs: list[tuple[KGNode, KGNode, FileArtifact, int]] = []  # (Service, Class, def art, line)

        def prov(art, line, evidence, conf=None, detail_extra=None):
            detail = {"language": art.language, "extractor": "code"}
            if detail_extra:
                detail.update(detail_extra)
            return Provenance(
                source_type=SourceType.SOURCE_CODE,
                source_id=f"{art.path}:{line}",
                evidence=evidence,
                confidence=conf if conf is not None else 0.95,  # SCIP/Joern precise resolution
                detail=detail,
            )

        # --------------------------------------------------------------- pass 1: nodes
        # Every Module/Class/Service/Function node is created before any structural
        # edge, so ``_ensure`` first-writer-wins never buries a full definition under
        # a minimal edge-endpoint stub -- the ordering trap this two-pass shape avoids.
        for art in artifacts:
            module_nodes[art.path] = self._ensure(
                result, node_index, NodeType.MODULE, art.module, art.module,
                {"path": art.path}, [prov(art, 1, f"module {art.module}")])

            by_name: dict[str, KGNode] = {}
            for c in art.classes:
                is_service = c.name in services
                metrics = c.metrics.to_dict()
                cls_node = self._ensure(
                    result, node_index, NodeType.CLASS, c.qualified_name, c.name,
                    {"file": art.path, "line": c.line, "qualified_name": c.qualified_name,
                     "bases": c.bases, "num_methods": len(c.methods),
                     "metrics": metrics, "is_service": is_service},
                    [prov(art, c.line, f"class {c.name}")], authoritative=True)
                by_name[c.name] = cls_node
                class_by_qname[c.qualified_name] = cls_node
                prev = class_by_simple.get(c.name, cls_node)
                class_by_simple[c.name] = None if prev is not cls_node else cls_node
                if is_service:
                    # Business/deployable overlay: the Service node keeps the simple-
                    # name canonical used by CALLS/EXPOSES/DEPLOYED_AS and links to its
                    # code Class via IMPLEMENTS (wired in pass 2).
                    svc = self._ensure(
                        result, node_index, NodeType.SERVICE, c.name, c.name,
                        {"file": art.path, "class": c.qualified_name, "bases": c.bases,
                         "num_methods": len(c.methods), "metrics": metrics},
                        [prov(art, c.line, f"class {c.name}")], authoritative=True)
                    service_class_pairs.append((svc, cls_node, art, c.line))
            classes_by_art[art.path] = by_name

            for f in art.functions:
                owner_cls = by_name.get(f.class_name) if f.class_name else None
                fn_attrs = {"file": art.path, "line": f.line, "class": f.class_name,
                            "class_qname": owner_cls.canonical_name if owner_cls else None,
                            "async": f.is_async, "params": f.params, "metrics": f.metrics.to_dict()}
                fn_node = self._ensure(
                    result, node_index, NodeType.FUNCTION, f.qualified_name, f.name,
                    fn_attrs, [prov(art, f.line, f"def {f.name}")], authoritative=True)
                functions_by_qname[f.qualified_name] = fn_node
                if f.metrics.clone_hash and (f.metrics.clone_token_count or 0) >= CLONE_MIN_TOKENS:
                    clone_groups.setdefault(f.metrics.clone_hash, []).append(
                        (fn_node, art, f.line, f.metrics.clone_token_count)
                    )

        # ---------------------------------------------------------------- pass 2: edges
        def _resolve_base_class(b, art):
            by_name = classes_by_art.get(art.path, {})
            if b in by_name:
                return by_name[b]
            if b in class_by_qname:
                return class_by_qname[b]
            return class_by_simple.get(b)

        def _handler_class(handler_qname, by_name):
            if not handler_qname:
                return None
            segs = set(handler_qname.split("."))
            for cname, cnode in by_name.items():
                if cname in segs:
                    return cnode
            return None

        # Container tree: Module -> Class, Class -> Function/Module-level Function,
        # Class -> Class INHERITS, and Service -> Class IMPLEMENTS (business overlay).
        for svc, cls_node, def_art, def_line in service_class_pairs:
            self._edge(result, svc.id, cls_node.id, RelationType.IMPLEMENTS, {},
                       [prov(def_art, def_line, f"{svc.name} implemented by {cls_node.canonical_name}")])

        for art in artifacts:
            module_node = module_nodes[art.path]
            by_name = classes_by_art[art.path]

            for c in art.classes:
                cls_node = by_name[c.name]
                self._edge(result, module_node.id, cls_node.id, RelationType.CONTAINS,
                           {"kind": "class"}, [prov(art, c.line, f"{art.module} defines {c.name}")])
                # INHERITS is now class-level: link to any in-repo base class.
                for b in c.bases:
                    parent = _resolve_base_class(b, art)
                    if parent is not None and parent.id != cls_node.id:
                        self._edge(result, cls_node.id, parent.id, RelationType.INHERITS, {},
                                   [prov(art, c.line, f"class {c.name}({b})")])

            for f in art.functions:
                fn_node = functions_by_qname[f.qualified_name]
                owner_cls = by_name.get(f.class_name) if f.class_name else None
                if owner_cls is not None:
                    self._edge(result, owner_cls.id, fn_node.id, RelationType.CONTAINS,
                               {"kind": "method"}, [prov(art, f.line, f"{f.class_name}.{f.name}")])
                else:
                    self._edge(result, module_node.id, fn_node.id, RelationType.CONTAINS,
                               {"kind": "function"}, [prov(art, f.line, f"{art.module}.{f.name}")])

            # Routes -> API: Class CONTAINS API (structural home) + Service EXPOSES API.
            for r in art.routes:
                api_name = _api_label(r.http_method, r.path, r.handler_qname)
                api_node = self._ensure(result, node_index, NodeType.API, api_name.lower(), api_name,
                                        {"method": r.http_method, "path": r.path, "framework": r.framework,
                                         "file": art.path, "handler_qname": r.handler_qname},
                                        [prov(art, r.line, f"{r.http_method} {r.path or ''} [{r.framework}]")])
                hcls = _handler_class(r.handler_qname, by_name)
                if hcls is not None:
                    self._edge(result, hcls.id, api_node.id, RelationType.CONTAINS,
                               {"kind": "api"}, [prov(art, r.line, f"{hcls.name} defines {api_name}")])
                owner = self._owner_service_of_handler(r.handler_qname, services, art)
                if owner:
                    svc = node_index[_key(NodeType.SERVICE, owner)]
                    self._edge(result, svc.id, api_node.id, RelationType.EXPOSES,
                               {"method": r.http_method, "path": r.path}, [prov(art, r.line, f"exposes {r.path}")])

            # Resources -> DB / ExternalDependency.
            for res in art.resources:
                if res.category in ("database", "cache"):
                    ntype, rel = NodeType.DATABASE, (
                        RelationType.WRITES if res.operation == "write"
                        else RelationType.READS if res.operation == "read"
                        else RelationType.DEPENDS_ON
                    )
                    rn = self._ensure(result, node_index, ntype, res.target.lower(), res.target,
                                      {"category": res.category}, [prov(art, res.line, res.evidence or res.target)])
                else:
                    ntype = NodeType.EXTERNAL_DEPENDENCY
                    rn = self._ensure(result, node_index, ntype, res.target.lower(), res.target,
                                      {"category": res.category}, [prov(art, res.line, res.evidence or res.target)])
                    rel = RelationType.DEPENDS_ON
                owner = self._nearest_service(res.file, res.line, art, services)
                if owner:
                    svc = self._ensure(result, node_index, NodeType.SERVICE, owner, owner,
                                       {"file": art.path}, [prov(art, res.line, f"{owner} touches {res.target}")])
                    self._edge(result, svc.id, rn.id, rel, {"operation": res.operation},
                               [prov(art, res.line, res.evidence or f"{owner}->{res.target}")])
                else:
                    self._edge(result, module_node.id, rn.id, RelationType.DEPENDS_ON,
                               {"operation": res.operation}, [prov(art, res.line, res.evidence or "")])

            # Configs: an owning service reads it -> Service --CONFIGURED_BY--> Config;
            # a module-level constant is structurally contained -> Module --CONTAINS--> Config.
            # Keys mirror the document-side Config nodes (value/source_file) so semantic
            # enrichment and cross-source merges see the same fields; ``default`` is kept.
            for cfg in art.configs:
                cfg_node = self._ensure(result, node_index, NodeType.CONFIG, cfg.key, cfg.key,
                                        {"value": cfg.default, "default": cfg.default,
                                         "source_file": art.path},
                                        [prov(art, cfg.line, cfg.evidence or cfg.key)])
                owner = self._nearest_service(cfg.file, cfg.line, art, services)
                if owner:
                    svc = self._ensure(result, node_index, NodeType.SERVICE, owner, owner,
                                       {"file": art.path},
                                       [prov(art, cfg.line, f"{owner} reads {cfg.key}")])
                    self._edge(result, svc.id, cfg_node.id, RelationType.CONFIGURED_BY,
                               {}, [prov(art, cfg.line, cfg.evidence or cfg.key)])
                else:
                    self._edge(result, module_node.id, cfg_node.id, RelationType.CONTAINS,
                               {"kind": "config"}, [prov(art, cfg.line, cfg.evidence or cfg.key)])

        # CALLS edges: SCIP-resolved calls only (no heuristic fallback).
        service_call_pairs: set[tuple[str, str]] = set()
        fn_call_pairs: set[tuple[str, str]] = set()
        route_index: list[tuple[list[str], str]] = []
        for art in artifacts:
            for r in art.routes:
                owner = self._owner_service_of_handler(r.handler_qname, services, art)
                npath = _norm_http_path(r.path or "")
                if owner and npath:
                    route_index.append(([s for s in npath.split("/") if s], owner))

        for art in artifacts:
            for call in art.calls:
                # Check if SCIP resolved this call (attached by ir_compat).
                scip_res = getattr(call, "_scip_resolved", None)
                caller_service = _class_of(call.caller_qname, services) if call.caller_qname else None

                # Function-level CALLS: only when SCIP resolved to a known function.
                callee_fn = None
                if scip_res:
                    callee_fn = _find_function_qname(scip_res.callee_qname, artifacts)
                if call.caller_qname and callee_fn and call.caller_qname != callee_fn:
                    a = self._ensure(result, node_index, NodeType.FUNCTION, call.caller_qname,
                                      call.caller_qname.split(".")[-1], {"file": art.path},
                                      [prov(art, call.line, f"calls {call.callee_name}",
                                            detail_extra={"resolved_by": "scip_symbol"})])
                    b = self._ensure(result, node_index, NodeType.FUNCTION, callee_fn,
                                     callee_fn.split(".")[-1], {"file": art.path},
                                     [prov(art, call.line, f"called by {call.caller_qname}",
                                           detail_extra={"resolved_by": "scip_symbol"})])
                    if (a.id, b.id) not in fn_call_pairs:
                        fn_call_pairs.add((a.id, b.id))
                        self._edge(result, a.id, b.id, RelationType.CALLS, {"resolved_by": "scip_symbol"},
                                   [prov(art, call.line, f"{call.caller_qname} -> {call.callee_name}",
                                         detail_extra={"resolved_by": "scip_symbol"})])

                # Service-level CALLS.
                callee_service = None
                if scip_res:
                    callee_service = _class_of(scip_res.callee_qname, services)
                if caller_service and callee_service and caller_service != callee_service:
                    s = self._ensure(result, node_index, NodeType.SERVICE, caller_service, caller_service,
                                     {"file": art.path},
                                     [prov(art, call.line, f"{caller_service} calls",
                                           detail_extra={"resolved_by": "scip_symbol"})])
                    t = self._ensure(result, node_index, NodeType.SERVICE, callee_service, callee_service,
                                     {"file": art.path},
                                     [prov(art, call.line, f"called by {caller_service}",
                                           detail_extra={"resolved_by": "scip_symbol"})])
                    if (s.id, t.id) not in service_call_pairs:
                        service_call_pairs.add((s.id, t.id))
                        self._edge(result, s.id, t.id, RelationType.CALLS,
                                   {"level": "service", "resolved_by": "scip_symbol"},
                                   [prov(art, call.line, f"{caller_service} -> {callee_service} ({call.callee_name})",
                                         0.95, {"resolved_by": "scip_symbol"})])
                # HTTP bridge fallback.
                elif caller_service and not callee_service and route_index:
                    target = self._match_route_service(call, route_index, caller_service)
                    if target:
                        s = self._ensure(result, node_index, NodeType.SERVICE, caller_service, caller_service,
                                         {"file": art.path},
                                         [prov(art, call.line, f"{caller_service} http-calls", _CONF_HTTP_BRIDGE,
                                               {"resolved_by": "http_route"})])
                        t = self._ensure(result, node_index, NodeType.SERVICE, target, target,
                                         {"file": art.path},
                                         [prov(art, call.line, f"{target} http-called", _CONF_HTTP_BRIDGE,
                                               {"resolved_by": "http_route"})])
                        if (s.id, t.id) not in service_call_pairs:
                            service_call_pairs.add((s.id, t.id))
                            self._edge(result, s.id, t.id, RelationType.CALLS,
                                       {"level": "http", "resolved_by": "http_route"},
                                       [prov(art, call.line, f"{caller_service} -> {target} via HTTP route",
                                             _CONF_HTTP_BRIDGE, {"resolved_by": "http_route"})])

        # Clone edges.
        clone_summary: list[dict] = []
        for clone_hash, group in clone_groups.items():
            uniq: dict[str, tuple] = {}
            for member in group:
                uniq.setdefault(member[0].id, member)
            members = list(uniq.values())
            if len(members) < 2:
                continue
            anchor = members[0]
            for other in members[1:]:
                self._edge(result, anchor[0].id, other[0].id, RelationType.CLONE_OF,
                           {"clone_hash": clone_hash, "tokens": anchor[3]},
                           [prov(other[1], other[2], f"clone of {anchor[0].name} ({clone_hash})")])
            files = sorted({a.path for _, a, _, _ in members})
            clone_summary.append({
                "clone_hash": clone_hash, "size": len(members),
                "distinct_files": len(files), "cross_file": len(files) > 1,
                "tokens": anchor[3], "files": files,
                "functions": sorted(m[0].canonical_name for m in members),
            })
        clone_summary.sort(key=lambda g: (g["size"], g["cross_file"]), reverse=True)
        result.extras["clone_groups"] = clone_summary

    # -------------------------------------------------------- emit helpers
    def _owner_service_of_handler(self, handler_qname, services, art):
        cls = _class_of(handler_qname, services)
        if cls:
            return cls
        in_file = [c.name for c in art.classes if c.name in services]
        return in_file[0] if len(in_file) == 1 else None

    @staticmethod
    def _match_route_service(call, route_index, caller_service):
        for hint in call.args:
            if hint.kind != "string" or not hint.value:
                continue
            npath = _norm_http_path(hint.value)
            if not npath:
                continue
            cand = [s for s in npath.split("/") if s]
            for rsegs, owner in route_index:
                if owner != caller_service and _route_matches(rsegs, cand):
                    return owner
        return None

    def _nearest_service(self, file_path, line, art, services):
        best, best_line = None, -1
        for f in art.functions:
            if f.class_name in services and f.line <= line and f.line > best_line:
                best, best_line = f.class_name, f.line
        if best:
            return best
        in_file = [c.name for c in art.classes if c.name in services]
        return in_file[0] if len(in_file) == 1 else None

    def _ensure(self, result, node_index, ntype, canon, name, attrs, provenance,
                authoritative=False):
        k = _key(ntype, canon)
        existing = node_index.get(k)
        if existing is not None:
            if authoritative and "metrics" not in existing.attributes:
                existing.attributes.update(attrs)
            for p in provenance:
                existing.add_provenance(p)
            return existing
        node = KGNode(
            id=make_node_id(ntype.value, canon, self.namespace),
            type=ntype.value, name=name, canonical_name=canon,
            namespace=self.namespace, attributes=attrs, provenance=provenance,
        )
        node_index[k] = node
        result.add_node(node)
        return node

    def _edge(self, result, source, target, relation, attrs, provenance):
        result.add_edge(KGEdge(source=source, target=target, relation=relation.value,
                               attributes=attrs, provenance=provenance))


# -------------------------------------------------------------- module funcs
def _key(ntype, canon):
    return f"{ntype.value}::{canon}"


def _class_of(qname, services):
    if not qname:
        return None
    parts = qname.split(".")
    for p in parts:
        if p in services:
            return p
    if parts[-1] in services:
        return parts[-1]
    # Suffix match for SCIP qnames with repo prefix.
    for svc in services:
        if qname.endswith("." + svc) or ("." + svc + ".") in qname:
            return svc
    return None


def _find_function_qname(callee_name, artifacts):
    """Check if callee_name matches any FunctionDef.qualified_name.

    Supports both exact match and suffix match (SCIP qnames may include
    repo/package prefixes not present in our module-derived qnames).
    """
    for art in artifacts:
        for f in art.functions:
            if f.qualified_name == callee_name:
                return f.qualified_name
            # Suffix match: SCIP "examples.sample_repo.services.helpers.fmt_order_id"
            # should match our "services.helpers.fmt_order_id".
            if callee_name.endswith("." + f.qualified_name):
                return f.qualified_name
    return None


def _api_label(method, path, handler):
    if path:
        return f"{method} {path}"
    return f"{method} {handler or 'api'}"


def _norm_http_path(raw):
    s = (raw or "").strip()
    if not s:
        return ""
    if "://" in s:
        s = s.split("://", 1)[1]
    s = re.split(r"[?#]", s, 1)[0]
    if "/" not in s:
        return ""
    path = "/" + s.split("/", 1)[1]
    out = []
    for seg in path.split("/"):
        if not seg:
            continue
        out.append("*" if seg[0] in "{:" else seg.lower())
    return "/" + "/".join(out) if out else ""


def _route_matches(route_segs, cand_segs):
    if len(route_segs) != len(cand_segs):
        return False
    return all(r == "*" or r == c for r, c in zip(route_segs, cand_segs))
