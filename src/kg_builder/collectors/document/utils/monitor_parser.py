"""Monitoring & observability config parser.

Handles Prometheus scrape configs / alert rules and Grafana dashboard JSON.
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


class MonitorParser(BaseConfigParser):
    category = "monitor"

    @staticmethod
    def can_parse(rel_path: str, ext: str, content: str) -> bool:
        name = rel_path.rsplit("/", 1)[-1].lower()
        # Prometheus files
        if any(k in name for k in ("prometheus", "alert_rules", "alerts", "scrape")):
            return ext in (".yaml", ".yml")
        # Grafana dashboard JSON
        if ext == ".json":
            head = content[:500]
            return '"panels"' in head or '"datasources"' in head or '"dashboard"' in head
        return False

    def parse(self, rel_path: str, content: str, owner_service: Optional[str],
              namespace: str) -> ParseResult:
        result = ParseResult()
        prov = Provenance(
            source_type=SourceType.DOCUMENT,
            source_id=f"doc_parser:monitor:{rel_path}",
            evidence=f"Monitoring config from {rel_path}",
            confidence=0.9,
            detail={"parser": "monitor", "file": rel_path},
        )

        if rel_path.lower().endswith(".json"):
            self._parse_grafana(result, rel_path, content, owner_service, namespace, prov)
        else:
            self._parse_prometheus(result, rel_path, content, owner_service, namespace, prov)

        return result

    def _parse_prometheus(self, result: ParseResult, rel_path: str, content: str,
                          owner_service: Optional[str], namespace: str, prov: Provenance):
        try:
            data = yaml.safe_load(content)
        except Exception as exc:
            result.warnings.append(f"prometheus parse failed: {rel_path}: {exc}")
            return

        if not isinstance(data, dict):
            return

        # Alert rules
        groups = data.get("groups", [])
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                rules = group.get("rules", [])
                for rule in rules:
                    if not isinstance(rule, dict):
                        continue
                    alert_name = rule.get("alert", "")
                    expr = rule.get("expr", "")
                    if alert_name:
                        node_id = make_node_id(NodeType.METRIC.value, f"alert/{alert_name}", namespace)
                        result.nodes.append(KGNode(
                            id=node_id, type=NodeType.METRIC.value, name=alert_name,
                            canonical_name=f"alert/{alert_name}", namespace=namespace,
                            attributes={"kind": "alert", "expr": str(expr)[:200], "source_file": rel_path},
                            provenance=[prov],
                        ))

        # Scrape configs
        scrape_configs = data.get("scrape_configs", [])
        if isinstance(scrape_configs, list):
            for sc in scrape_configs:
                if not isinstance(sc, dict):
                    continue
                job = sc.get("job_name", "")
                targets = sc.get("static_configs", [])
                if job:
                    node_id = make_node_id(NodeType.CONFIG.value, f"scrape/{job}", namespace)
                    result.nodes.append(KGNode(
                        id=node_id, type=NodeType.CONFIG.value, name=f"scrape:{job}",
                        canonical_name=f"scrape/{job}", namespace=namespace,
                        attributes={"kind": "scrape_config", "target_count": len(targets), "source_file": rel_path},
                        provenance=[prov],
                    ))

    def _parse_grafana(self, result: ParseResult, rel_path: str, content: str,
                       owner_service: Optional[str], namespace: str, prov: Provenance):
        try:
            data = json.loads(content)
        except Exception as exc:
            result.warnings.append(f"grafana parse failed: {rel_path}: {exc}")
            return

        if not isinstance(data, dict):
            return

        title = data.get("title", data.get("dashboard", {}).get("title", rel_path))
        panels = data.get("panels", data.get("dashboard", {}).get("panels", []))

        node_id = make_node_id("Dashboard", title, namespace)
        result.nodes.append(KGNode(
            id=node_id, type="Dashboard", name=title,
            canonical_name=title, namespace=namespace,
            attributes={
                "panel_count": len(panels) if isinstance(panels, list) else 0,
                "source_file": rel_path,
            },
            provenance=[prov],
        ))
