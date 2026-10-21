"""Semantic enrichment layer -- LLM-only.

Given the merged graph, this attaches *inferred semantics* that static analysis
and the raw sources do not provide. Per the design, these are exactly the
judgements a model is good at and rule-based tools are not:

* Service  -> business domain + responsibility + whether it sits on a core link
* API      -> whether it belongs to a core business chain (+ short summary)
* Config   -> what runtime behaviour the key controls

There is **no heuristic fallback**: every semantic annotation comes from the LLM
client. If the model is unavailable / misconfigured, :class:`LLMError` propagates
and the build fails loudly (callers may skip enrichment entirely instead).

All model output is recorded as graph provenance with ``source=llm`` and a
confidence of ``0.7`` -- deliberately lower than observed facts, so Module 2 can
weigh inference against evidence.
"""
from __future__ import annotations

import json
from typing import Any

from ..graph.knowledge_graph import KnowledgeGraph
from ..graph.model import Provenance
from ..graph.ontology import NodeType, RelationType, SourceType
from .llm_client import LLMClient

_SYSTEM = (
    "你是资深软件架构分析师。你只依据我给的结构信息做语义判断，不臆造具体实现。"
    "严格按要求的 JSON 格式输出，键与输入完全一致，不要添加解释文字。"
)
_CONFIDENCE = 0.7


class SemanticEnricher:
    def __init__(self, llm: LLMClient, namespace: str = "", max_items_per_call: int = 40) -> None:
        self.llm = llm
        self.namespace = namespace
        self.max_items_per_call = max_items_per_call

    # ------------------------------------------------------------------ run
    def enrich(self, graph: KnowledgeGraph) -> dict[str, Any]:
        summary: dict[str, Any] = {"llm": True}
        services = graph.find_nodes(type=NodeType.SERVICE)
        apis = graph.find_nodes(type=NodeType.API)
        configs = graph.find_nodes(type=NodeType.CONFIG)

        summary["services"] = self._annotate_services(graph, services)
        summary["apis"] = self._annotate_apis(graph, apis)
        summary["configs"] = self._annotate_configs(graph, configs)
        return summary

    # -------------------------------------------------------------- services
    def _annotate_services(self, graph: KnowledgeGraph, services: list) -> int:
        calls_out: dict[str, list[str]] = {}
        calls_in: dict[str, list[str]] = {}
        exposes: dict[str, list[str]] = {}
        for e in graph.edges:
            if e.relation == RelationType.CALLS.value:
                src = graph.get_node(e.source)
                dst = graph.get_node(e.target)
                if src and dst and src.type == NodeType.SERVICE.value and dst.type == NodeType.SERVICE.value:
                    calls_out.setdefault(src.canonical_name, []).append(dst.canonical_name)
                    calls_in.setdefault(dst.canonical_name, []).append(src.canonical_name)
            elif e.relation == RelationType.EXPOSES.value:
                src = graph.get_node(e.source)
                dst = graph.get_node(e.target)
                if src and dst:
                    exposes.setdefault(src.canonical_name, []).append(dst.name)

        annotated = 0
        for chunk in _chunks(services, self.max_items_per_call):
            listing = "\n".join(_dump_service(graph, s, calls_out, calls_in, exposes) for s in chunk)
            user = (
                "请分析下列服务，判断其业务领域、是否为【核心业务链路】（如下单、支付、结算、鉴权、履约等直接影响主流程的环节），"
                "并用一句话概括其职责。输出 JSON：键为服务名，值为 "
                '{"business_domain": str, "is_core_business": bool, "responsibility": str}。\n\n'
                f"服务列表：\n{listing}"
            )
            result = self.llm.complete_json(_SYSTEM, user)
            for s in chunk:
                opinion = result.get(s.canonical_name)
                if not isinstance(opinion, dict):
                    continue
                merged = dict(s.attributes.get("semantic", {}))
                for k in ("business_domain", "responsibility"):
                    if opinion.get(k):
                        merged[k] = str(opinion[k])
                if "is_core_business" in opinion:
                    merged["is_core_business"] = bool(opinion["is_core_business"])
                s.attributes["semantic"] = merged
                self._attach(s, json.dumps(opinion, ensure_ascii=False))
                annotated += 1
        return annotated

    # ------------------------------------------------------------------- apis
    def _annotate_apis(self, graph: KnowledgeGraph, apis: list) -> int:
        owner_of = {e.target: e.source for e in graph.edges if e.relation == RelationType.EXPOSES.value}
        annotated = 0
        for chunk in _chunks(apis, self.max_items_per_call):
            lines = []
            for a in chunk:
                owner = graph.get_node(owner_of.get(a.id, ""))
                lines.append(f"- {a.name} (method={a.attributes.get('method')}, path={a.attributes.get('path')}, "
                             f"owner_service={owner.canonical_name if owner else None})")
            listing = "\n".join(lines)
            user = (
                "请判断下列接口是否属于【核心业务链路】，并给出一句话摘要。"
                '输出 JSON：键为接口名（与输入完全一致），值为 {"is_core_business_link": bool, "summary": str}。\n\n'
                f"接口列表：\n{listing}"
            )
            result = self.llm.complete_json(_SYSTEM, user)
            for a in chunk:
                opinion = result.get(a.name)
                if not isinstance(opinion, dict):
                    continue
                merged = dict(a.attributes.get("semantic", {}))
                if "is_core_business_link" in opinion:
                    merged["is_core_business_link"] = bool(opinion["is_core_business_link"])
                if opinion.get("summary"):
                    merged["summary"] = str(opinion["summary"])
                a.attributes["semantic"] = merged
                self._attach(a, json.dumps(opinion, ensure_ascii=False))
                annotated += 1
        return annotated

    # --------------------------------------------------------------- configs
    def _annotate_configs(self, graph: KnowledgeGraph, configs: list) -> int:
        annotated = 0
        for chunk in _chunks(configs, self.max_items_per_call):
            listing = "\n".join(
                f"- {c.canonical_name} = {c.attributes.get('value')} (file={c.attributes.get('source_file')})"
                for c in chunk
            )
            user = (
                "请推断下列配置项各自控制什么运行时行为。"
                '输出 JSON：键为配置项名（与输入完全一致），值为 {"controls": str, "category": str}，'
                "category 取如 timeout/retry/limit/switch/connection/cache 等简短类别。\n\n"
                f"配置项列表：\n{listing}"
            )
            result = self.llm.complete_json(_SYSTEM, user)
            for c in chunk:
                opinion = result.get(c.canonical_name)
                if not isinstance(opinion, dict):
                    continue
                merged = dict(c.attributes.get("semantic", {}))
                if opinion.get("controls"):
                    merged["controls"] = str(opinion["controls"])
                if opinion.get("category"):
                    merged["category"] = str(opinion["category"])
                c.attributes["semantic"] = merged
                self._attach(c, json.dumps(opinion, ensure_ascii=False))
                annotated += 1
        return annotated

    # --------------------------------------------------------------- helpers
    def _attach(self, node, evidence: str) -> None:
        node.add_provenance(
            Provenance(
                source_type=SourceType.LLM,
                source_id="semantic://llm",
                evidence=evidence,
                confidence=_CONFIDENCE,
                detail={"method": "llm", "model": getattr(self.llm, "_settings", None) and self.llm._settings.model},
            )
        )


def _dump_service(graph, s, calls_out, calls_in, exposes) -> str:
    return (
        f"- {s.canonical_name} (file={s.attributes.get('file')}, "
        f"calls={calls_out.get(s.canonical_name, [])}, called_by={calls_in.get(s.canonical_name, [])}, "
        f"exposes={exposes.get(s.canonical_name, [])})"
    )


def _chunks(items: list, size: int):
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i:i + size]
