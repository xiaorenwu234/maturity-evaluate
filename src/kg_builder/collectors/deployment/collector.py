"""Deployment collector -- orchestrates K8s and Alibaba Cloud product collectors.

When ``settings.deployment.enabled`` is True, this collector iterates over all
enabled cloud product sub-collectors and merges their results into a single
``CollectorResult``.

Each sub-collector lives in ``aliyun/`` and follows the legacy CollectorBase
contract (returns DataSource with records). This module bridges those records
into KGNode/KGEdge for the knowledge graph.
"""
from __future__ import annotations

import logging
from typing import Any

from ...config import BuildSettings
from ...graph.model import KGEdge, KGNode, Provenance, make_node_id
from ...graph.ontology import NodeType, RelationType, SourceType
from ..base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)


class DeploymentCollector(BaseCollector):
    name = "deployment"

    def __init__(self, settings: BuildSettings) -> None:
        self.settings = settings
        self.dep = settings.deployment
        self.namespace = settings.namespace

    def is_enabled(self) -> bool:
        return self.dep.enabled

    def collect(self) -> CollectorResult:
        result = CollectorResult(collector=self.name)

        if not self.dep.enabled:
            return result

        collected: list[str] = []
        failed: list[str] = []

        # --- ACK / K8s ---
        if self.dep.cluster_id or self.dep.kubeconfig or self.dep.kubeconfig_paths:
            self._run_sub(result, "ack", self._collect_ack, collected, failed)

        # --- Alibaba Cloud products (each guarded by its own enabled flag) ---
        _PRODUCT_MAP = [
            ("ecs", self.dep.ecs.enabled, self._collect_ecs),
            ("rds", self.dep.rds.enabled, self._collect_rds),
            ("oss", self.dep.oss.enabled, self._collect_oss),
            ("sls", self.dep.sls.enabled, self._collect_sls),
            ("cms", self.dep.cms.enabled, self._collect_cms),
            ("ros", self.dep.ros.enabled, self._collect_ros),
            ("acr", self.dep.acr.enabled, self._collect_acr),
            ("alb", self.dep.alb.enabled, self._collect_alb),
            ("arms", self.dep.arms.enabled, self._collect_arms),
            ("fc", self.dep.fc.enabled, self._collect_fc),
            ("eventbridge", self.dep.eventbridge.enabled, self._collect_eventbridge),
            ("grafana", self.dep.grafana.enabled, self._collect_grafana),
            ("gtm", self.dep.gtm.enabled, self._collect_gtm),
            ("tair", self.dep.tair.enabled, self._collect_tair),
            ("codeup", self.dep.codeup.enabled, self._collect_codeup),
        ]
        for product_name, enabled, fn in _PRODUCT_MAP:
            if enabled:
                self._run_sub(result, product_name, fn, collected, failed)

        result.metrics["collected"] = collected
        result.metrics["failed"] = failed

        if not collected and not failed:
            result.warnings.append(
                "deployment collector is enabled but no cloud products are configured. "
                "Set deployment.<product>.enabled=true in config."
            )

        return result

    # ---------------------------------------------------------------------------
    # Sub-collector dispatch
    # ---------------------------------------------------------------------------

    def _run_sub(self, result: CollectorResult, name: str, fn, collected: list, failed: list):
        """Run a sub-collector, catch errors, merge nodes/edges into result."""
        try:
            nodes, edges = fn()
            for n in nodes:
                result.add_node(n)
            for e in edges:
                result.add_edge(e)
            collected.append(name)
            logger.info(f"deployment.{name}: {len(nodes)} nodes, {len(edges)} edges")
        except ImportError as exc:
            msg = f"deployment.{name}: SDK not installed ({exc})"
            result.warnings.append(msg)
            failed.append(name)
            logger.warning(msg)
        except Exception as exc:
            msg = f"deployment.{name}: collection failed ({exc})"
            result.warnings.append(msg)
            failed.append(name)
            logger.error(msg, exc_info=True)

    # ---------------------------------------------------------------------------
    # Record -> Graph bridge
    # ---------------------------------------------------------------------------

    def _records_to_graph(self, records: list[Any], source: str) -> tuple[list[KGNode], list[KGEdge]]:
        """Convert legacy DataSource records into KGNode/KGEdge.

        Each record dataclass is mapped to a node based on its type name.
        This is a generic bridge; product-specific edge logic can be added
        per record type as needed.
        """
        nodes: list[KGNode] = []
        edges: list[KGEdge] = []

        for rec in records:
            type_name = type(rec).__name__
            prov = Provenance(
                source_type=SourceType.DEPLOYMENT,
                source_id=f"{source}/{type_name}",
                evidence=str(rec)[:200],
                confidence=1.0,
                detail={"collector": f"deployment.{source}", "record_type": type_name},
            )

            # Determine node type and canonical name from record fields
            node_type, canon, name = self._classify_record(rec, type_name, source)
            node_id = make_node_id(node_type, canon, self.namespace)

            # Collect all dataclass fields as attributes
            attrs: dict[str, Any] = {"cloud_provider": "alibaba", "service": source}
            if hasattr(rec, "__dataclass_fields__"):
                for fname in rec.__dataclass_fields__:
                    val = getattr(rec, fname, None)
                    if val is not None:
                        attrs[fname] = val

            nodes.append(KGNode(
                id=node_id, type=node_type, name=name,
                canonical_name=canon, namespace=self.namespace,
                attributes=attrs, provenance=[prov],
            ))

        return nodes, edges

    @staticmethod
    def _classify_record(rec, type_name: str, source: str) -> tuple[str, str, str]:
        """Map a record dataclass to (node_type, canonical_name, display_name)."""
        # K8s records
        if "Deployment" in type_name:
            ns = getattr(rec, "namespace", "")
            nm = getattr(rec, "name", type_name)
            return NodeType.DEPLOYMENT.value, f"{ns}/{nm}", nm
        if "StatefulSet" in type_name:
            ns = getattr(rec, "namespace", "")
            nm = getattr(rec, "name", type_name)
            return NodeType.DEPLOYMENT.value, f"{ns}/{nm}", nm
        if "Pod" in type_name and "Probe" not in type_name:
            ns = getattr(rec, "namespace", "")
            nm = getattr(rec, "name", type_name)
            return "Pod", f"{ns}/{nm}", nm
        if "Service" in type_name and "K8s" in type_name:
            ns = getattr(rec, "namespace", "")
            nm = getattr(rec, "name", type_name)
            return NodeType.SERVICE.value, nm, nm
        if "Ingress" in type_name:
            ns = getattr(rec, "namespace", "")
            nm = getattr(rec, "name", type_name)
            return NodeType.API.value, f"{ns}/{nm}", nm
        if "Hpa" in type_name or "Vpa" in type_name:
            ns = getattr(rec, "namespace", "")
            nm = getattr(rec, "name", type_name)
            return "AutoScaling", f"{ns}/{nm}", nm
        if "Node" in type_name and "K8s" in type_name:
            nm = getattr(rec, "name", type_name)
            return "Infrastructure", f"k8s-node/{nm}", nm

        # ECS
        if "EcsInstance" in type_name:
            iid = getattr(rec, "instance_id", type_name)
            nm = getattr(rec, "instance_name", iid)
            return "Infrastructure", f"ecs/{iid}", nm
        if "SecurityGroup" in type_name and "Rule" not in type_name:
            sgid = getattr(rec, "security_group_id", type_name)
            nm = getattr(rec, "security_group_name", sgid)
            return "Infrastructure", f"sg/{sgid}", nm

        # RDS
        if "RdsInstance" in type_name:
            iid = getattr(rec, "instance_id", type_name)
            nm = getattr(rec, "instance_name", iid) if hasattr(rec, "instance_name") else iid
            return NodeType.DATABASE.value, f"rds/{iid}", nm

        # OSS
        if "OssBucket" in type_name and "Lifecycle" not in type_name:
            nm = getattr(rec, "bucket_name", type_name)
            return NodeType.EXTERNAL_DEPENDENCY.value, f"oss/{nm}", nm

        # SLS
        if "SlsLogstore" in type_name:
            nm = getattr(rec, "logstore_name", type_name)
            return NodeType.EXTERNAL_DEPENDENCY.value, f"sls/{nm}", nm

        # ACR
        if "AcrRepository" in type_name:
            nm = getattr(rec, "repo_name", type_name)
            return NodeType.EXTERNAL_DEPENDENCY.value, f"acr/{nm}", nm

        # ALB
        if "AlbListener" in type_name:
            nm = getattr(rec, "listener_id", type_name)
            return NodeType.API.value, f"alb/{nm}", nm

        # FC
        if "FcFunction" in type_name:
            nm = getattr(rec, "function_name", type_name)
            return NodeType.SERVICE.value, f"fc/{nm}", nm

        # EventBridge
        if "EventBus" in type_name:
            nm = getattr(rec, "bus_name", type_name)
            return NodeType.EXTERNAL_DEPENDENCY.value, f"eb/{nm}", nm

        # Grafana
        if "GrafanaDashboard" in type_name:
            nm = getattr(rec, "title", type_name)
            return "Dashboard", f"grafana/{nm}", nm

        # Fallback
        name = getattr(rec, "name", None) or getattr(rec, "id", None) or type_name
        return "CloudResource", f"{source}/{name}", str(name)

    # ---------------------------------------------------------------------------
    # Individual sub-collector wrappers
    # ---------------------------------------------------------------------------

    def _get_creds(self):
        from .aliyun.base import get_credentials
        return get_credentials(self.dep)

    def _collect_ack(self):
        from .aliyun.ack_collector import ACKCollector, ACKCollectorConfig
        config = ACKCollectorConfig(
            aliyun_credentials=self._get_creds(),
            region=self.dep.aliyun_region,
            cluster_id=self.dep.cluster_id,
            kubeconfig_paths=self.dep.kubeconfig_paths,
            namespaces=self.dep.namespaces,
        )
        ds = ACKCollector(config).collect()
        return self._records_to_graph(ds.records, "ack")

    def _collect_ecs(self):
        from .aliyun.ecs_collector import ECSCollector, ECSCollectorConfig
        config = ECSCollectorConfig(aliyun_credentials=self._get_creds(), region=self.dep.aliyun_region)
        ds = ECSCollector(config).collect()
        return self._records_to_graph(ds.records, "ecs")

    def _collect_rds(self):
        from .aliyun.rds_collector import RDSCollector, RDSCollectorConfig
        config = RDSCollectorConfig(
            aliyun_credentials=self._get_creds(),
            region=self.dep.aliyun_region,
            rds_region=self.dep.rds.region or self.dep.aliyun_region,
            rds_instance_ids=self.dep.rds.instance_ids,
        )
        ds = RDSCollector(config).collect()
        return self._records_to_graph(ds.records, "rds")

    def _collect_oss(self):
        from .aliyun.oss_collector import OSSCollector, OSSCollectorConfig
        config = OSSCollectorConfig(
            aliyun_credentials=self._get_creds(),
            region=self.dep.oss.region or self.dep.aliyun_region,
            oss_bucket_names=self.dep.oss.bucket_names,
        )
        ds = OSSCollector(config).collect()
        return self._records_to_graph(ds.records, "oss")

    def _collect_sls(self):
        from .aliyun.sls_collector import SLSCollector, SLSCollectorConfig
        config = SLSCollectorConfig(
            aliyun_credentials=self._get_creds(),
            sls_project=self.dep.sls.project,
            sls_region=self.dep.sls.region or self.dep.aliyun_region,
        )
        ds = SLSCollector(config).collect()
        return self._records_to_graph(ds.records, "sls")

    def _collect_cms(self):
        from .aliyun.cms_collector import CMSCollector, CMSCollectorConfig
        config = CMSCollectorConfig(aliyun_credentials=self._get_creds())
        ds = CMSCollector(config).collect()
        return self._records_to_graph(ds.records, "cms")

    def _collect_ros(self):
        from .aliyun.ros_collector import ROSCollector, ROSCollectorConfig
        config = ROSCollectorConfig(
            aliyun_credentials=self._get_creds(),
            region=self.dep.aliyun_region,
            ros_region=self.dep.ros.region or self.dep.aliyun_region,
            ros_stack_name=self.dep.ros.stack_names or None,
        )
        ds = ROSCollector(config).collect()
        return self._records_to_graph(ds.records, "ros")

    def _collect_acr(self):
        from .aliyun.acr_collector import ACRCollector, ACRCollectorConfig
        config = ACRCollectorConfig(
            aliyun_credentials=self._get_creds(),
            region=self.dep.aliyun_region,
            instance_ids=self.dep.acr.instance_ids,
            otel_only=self.dep.acr.otel_only,
        )
        ds = ACRCollector(config).collect()
        return self._records_to_graph(ds.records, "acr")

    def _collect_alb(self):
        from .aliyun.alb_collector import ALBCollector, ALBCollectorConfig
        config = ALBCollectorConfig(
            aliyun_credentials=self._get_creds(),
            region=self.dep.aliyun_region,
            load_balancer_ids=self.dep.alb.load_balancer_ids,
        )
        ds = ALBCollector(config).collect()
        return self._records_to_graph(ds.records, "alb")

    def _collect_arms(self):
        from .aliyun.arms_collector import ARMSCollector, ARMSCollectorConfig
        config = ARMSCollectorConfig(aliyun_credentials=self._get_creds(), region=self.dep.aliyun_region)
        ds = ARMSCollector(config).collect()
        return self._records_to_graph(ds.records, "arms")

    def _collect_fc(self):
        from .aliyun.fc_collector import FCCollector, FCCollectorConfig
        config = FCCollectorConfig(
            aliyun_credentials=self._get_creds(),
            region=self.dep.aliyun_region,
            fc_function_names=self.dep.fc.function_names,
        )
        ds = FCCollector(config).collect()
        return self._records_to_graph(ds.records, "fc")

    def _collect_eventbridge(self):
        from .aliyun.eventbridge_collector import EventBridgeCollector, EventBridgeCollectorConfig
        config = EventBridgeCollectorConfig(
            aliyun_credentials=self._get_creds(),
            region=self.dep.aliyun_region,
            eventbridge_bus_names=self.dep.eventbridge.bus_names,
        )
        ds = EventBridgeCollector(config).collect()
        return self._records_to_graph(ds.records, "eventbridge")

    def _collect_grafana(self):
        import os
        from .aliyun.grafana_collector import GrafanaCollector, GrafanaCollectorConfig
        token = os.getenv(self.dep.grafana.api_token_env, "")
        config = GrafanaCollectorConfig(
            grafana_url=self.dep.grafana.url,
            grafana_api_token=token,
            grafana_workspace_id=self.dep.grafana.workspace_id,
            grafana_folder_ids=self.dep.grafana.folder_ids,
            grafana_tags=self.dep.grafana.tags,
        )
        ds = GrafanaCollector(config).collect()
        return self._records_to_graph(ds.records, "grafana")

    def _collect_gtm(self):
        from .aliyun.gtm_collector import GTMCollector, GTMCollectorConfig
        config = GTMCollectorConfig(aliyun_credentials=self._get_creds())
        ds = GTMCollector(config).collect()
        return self._records_to_graph(ds.records, "gtm")

    def _collect_tair(self):
        from .aliyun.tair_collector import TairCollector, TairCollectorConfig
        config = TairCollectorConfig(
            aliyun_credentials=self._get_creds(),
            region=self.dep.aliyun_region,
            tair_instance_ids=self.dep.tair.instance_ids,
        )
        ds = TairCollector(config).collect()
        return self._records_to_graph(ds.records, "tair")

    def _collect_codeup(self):
        import os
        from .aliyun.codeup_collector import CodeupCollector, CodeupCollectorConfig
        token = os.getenv(self.dep.codeup.yunxiao_token_env, "")
        config = CodeupCollectorConfig(
            aliyun_credentials=self._get_creds(),
            yunxiao_token=token,
            codeup_org_id=self.dep.codeup.org_id,
            codeup_project_name=self.dep.codeup.project_name,
        )
        ds = CodeupCollector(config).collect()
        return self._records_to_graph(ds.records, "codeup")
