"""Container-tree topology for the code collector ``_emit``.

Validates the Class-node refactor: the structural tree is
``Module --CONTAINS--> Class --CONTAINS--> Function/API``, module-level
functions/constants hang off the Module, inheritance is class-level, and a
service class gets a business ``Service`` overlay bound to its code ``Class``
via ``IMPLEMENTS``. The legacy inverted/mixed edges
(``Service --DEPENDS_ON{contains}--> Module`` and
``Service --DEPENDS_ON{owns}--> Function``) must no longer appear.

These tests drive ``_emit`` directly with hand-built IR (``FileArtifact``), so
they need neither scip-python nor semgrep -- the assertions are purely about the
graph-mapping layer.
"""
from __future__ import annotations

from pathlib import Path

from kg_builder.collectors.base import CollectorResult
from kg_builder.collectors.code.collector import CodeCollector
from kg_builder.collectors.code.schema.artifact import (
    ClassDef,
    ConfigRef,
    FileArtifact,
    FunctionDef,
    RouteDef,
)
from kg_builder.config import BuildSettings
from kg_builder.graph.ontology import NodeType, RelationType


def _collector() -> CodeCollector:
    return CodeCollector(BuildSettings(repo_path=Path(".")))


def _artifacts():
    path = "services/order.py"
    order = FileArtifact(
        path=path,
        language="python",
        module="services.order",
        classes=[
            ClassDef(qualified_name="services.order.BaseService", name="BaseService", file=path, line=1),
            ClassDef(
                qualified_name="services.order.OrderService", name="OrderService", file=path, line=5,
                bases=["BaseService"], methods=["__init__", "create_order"],
            ),
        ],
        functions=[
            FunctionDef(qualified_name="services.order.OrderService.__init__", name="__init__",
                        file=path, line=6, class_name="OrderService"),
            FunctionDef(qualified_name="services.order.OrderService.create_order", name="create_order",
                        file=path, line=8, class_name="OrderService"),
            # module-level function: no owning class
            FunctionDef(qualified_name="services.order.helper_fn", name="helper_fn",
                        file=path, line=20, class_name=None),
        ],
        routes=[
            RouteDef(framework="flask", http_method="POST", path="/order/create",
                     handler_qname="services.order.OrderService.create_order", file=path, line=8),
        ],
        # attributed to the enclosing service class (a service function precedes this line)
        configs=[ConfigRef(key="ORDER_TIMEOUT_SECONDS", file=path, line=12)],
        service_classes={"OrderService"},
    )
    settings = FileArtifact(
        path="settings.py",
        language="python",
        module="settings",
        # no classes / no service classes -> config is a module-level constant
        configs=[ConfigRef(key="CACHE_TTL", file="settings.py", line=2)],
    )
    return [order, settings]


def _run_emit():
    result = CollectorResult(collector="code")
    _collector()._emit(result, _artifacts(), services={"OrderService"})
    return result


def _by_id(result):
    return {n.id: n for n in result.nodes}


def _find_node(result, ntype, canon):
    return next((n for n in result.nodes if n.type == ntype.value and n.canonical_name == canon), None)


def _has_edge(result, rel, src_id, dst_id, kind=None):
    for e in result.edges:
        if e.relation == rel.value and e.source == src_id and e.target == dst_id:
            if kind is None or e.attributes.get("kind") == kind:
                return True
    return False


def test_class_nodes_created_for_every_class():
    result = _run_emit()
    assert _find_node(result, NodeType.CLASS, "services.order.OrderService") is not None
    # BaseService is NOT a service (services={"OrderService"}) yet still gets a Class node.
    assert _find_node(result, NodeType.CLASS, "services.order.BaseService") is not None


def test_module_contains_class_and_functions():
    result = _run_emit()
    ids = _by_id(result)
    module = _find_node(result, NodeType.MODULE, "services.order")
    order_cls = _find_node(result, NodeType.CLASS, "services.order.OrderService")
    helper = _find_node(result, NodeType.FUNCTION, "services.order.helper_fn")
    assert _has_edge(result, RelationType.CONTAINS, module.id, order_cls.id, kind="class")
    # module-level function hangs off the Module, not a Class
    assert _has_edge(result, RelationType.CONTAINS, module.id, helper.id, kind="function")
    assert ids[order_cls.id].type == NodeType.CLASS.value


def test_class_contains_methods_including_dunder_init():
    result = _run_emit()
    order_cls = _find_node(result, NodeType.CLASS, "services.order.OrderService")
    init_fn = _find_node(result, NodeType.FUNCTION, "services.order.OrderService.__init__")
    create_fn = _find_node(result, NodeType.FUNCTION, "services.order.OrderService.create_order")
    assert _has_edge(result, RelationType.CONTAINS, order_cls.id, init_fn.id, kind="method")
    assert _has_edge(result, RelationType.CONTAINS, order_cls.id, create_fn.id, kind="method")


def test_service_overlay_implements_class():
    result = _run_emit()
    svc = _find_node(result, NodeType.SERVICE, "OrderService")
    order_cls = _find_node(result, NodeType.CLASS, "services.order.OrderService")
    assert svc is not None, "service class must still produce a Service node"
    assert _has_edge(result, RelationType.IMPLEMENTS, svc.id, order_cls.id)


def test_inherits_is_class_level():
    result = _run_emit()
    order_cls = _find_node(result, NodeType.CLASS, "services.order.OrderService")
    base_cls = _find_node(result, NodeType.CLASS, "services.order.BaseService")
    assert _has_edge(result, RelationType.INHERITS, order_cls.id, base_cls.id)


def test_class_contains_api_and_service_exposes_api():
    result = _run_emit()
    order_cls = _find_node(result, NodeType.CLASS, "services.order.OrderService")
    svc = _find_node(result, NodeType.SERVICE, "OrderService")
    api = next((n for n in result.nodes if n.type == NodeType.API.value), None)
    assert api is not None
    assert _has_edge(result, RelationType.CONTAINS, order_cls.id, api.id, kind="api")
    assert _has_edge(result, RelationType.EXPOSES, svc.id, api.id)


def test_config_attachment_by_scope():
    result = _run_emit()
    # module-level constant -> Module CONTAINS Config
    settings_mod = _find_node(result, NodeType.MODULE, "settings")
    cache_ttl = _find_node(result, NodeType.CONFIG, "CACHE_TTL")
    assert _has_edge(result, RelationType.CONTAINS, settings_mod.id, cache_ttl.id, kind="config")
    # config read inside a service class -> Service CONFIGURED_BY Config
    svc = _find_node(result, NodeType.SERVICE, "OrderService")
    timeout = _find_node(result, NodeType.CONFIG, "ORDER_TIMEOUT_SECONDS")
    assert _has_edge(result, RelationType.CONFIGURED_BY, svc.id, timeout.id)


def test_no_legacy_depends_on_contains_or_owns():
    """The refactor must not emit the old inverted/mixed structural edges."""
    result = _run_emit()
    offending = [
        e for e in result.edges
        if e.relation == RelationType.DEPENDS_ON.value and e.attributes.get("kind") in {"owns", "contains"}
    ]
    assert offending == []
