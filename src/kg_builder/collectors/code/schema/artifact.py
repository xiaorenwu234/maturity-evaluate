"""Language-neutral intermediate representation (IR) for parsed source code.

Every extractor (Python-ast, Tree-sitter) normalises its findings into
these dataclasses. The static-analysis / framework-detection / graph-mapping
layers then operate purely on this IR, independent of the source language.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ArgHint:
    """A lightweight hint about a call argument (e.g. a URL/DB connection str)."""

    kind: str          # "string" | "name" | "other"
    value: str


@dataclass
class Metrics:
    """Quantitative "metrics bag" for one function or class.

    These are the inputs that metric-driven smell detectors (Long Method, God
    Class, Feature Envy, high complexity ...) consume. Every field is optional
    so extractors that cannot compute a given metric leave it ``None`` rather
    than guessing -- downstream code only trusts what was actually measured.

    Attributes:
        loc: non-blank, non-comment source lines of the body.
        cyclomatic_complexity: McCabe complexity (1 + decision points).
        cognitive_complexity: nesting-weighted complexity (approximate Sonar-style).
        num_params: number of declared parameters.
        num_fields: number of distinct ``self.x`` attributes assigned (classes).
        self_data_access: accesses to the class's own ``self.*`` state (Feature
            Envy numerator's counterpart).
        foreign_data_access: accesses to another object's data (candidate Feature
            Envy / data coupling signal).
    """

    loc: Optional[int] = None
    cyclomatic_complexity: Optional[int] = None
    cognitive_complexity: Optional[int] = None
    num_params: Optional[int] = None
    num_fields: Optional[int] = None
    self_data_access: Optional[int] = None
    foreign_data_access: Optional[int] = None
    # Control-flow shape (feeds Long Method / complexity smells).
    num_statements: Optional[int] = None
    num_branches: Optional[int] = None
    max_nesting_depth: Optional[int] = None
    # Type-2 clone fingerprint: identifiers/literals normalised away, structure kept.
    clone_hash: Optional[str] = None
    clone_token_count: Optional[int] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class FunctionDef:
    qualified_name: str          # module.Class.method or module.func
    name: str
    file: str
    line: int
    class_name: Optional[str] = None   # owning class for methods
    decorators: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    is_async: bool = False
    line_end: Optional[int] = None
    metrics: Metrics = field(default_factory=Metrics)
    # Optional statement-level control-flow graph (dict with nodes/edges),
    # produced by extractors that can build one (Python). None otherwise.
    cfg: Optional[dict] = None


@dataclass
class ClassDef:
    qualified_name: str
    name: str
    file: str
    line: int
    bases: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    line_end: Optional[int] = None
    metrics: Metrics = field(default_factory=Metrics)
    # Lightweight type inference: ``self.<field>`` -> inferred class/type name
    # (from ``self.x = Foo()``, ``self.x: Foo``, or ``self.x = <annotated param>``).
    field_types: dict[str, str] = field(default_factory=dict)


@dataclass
class CallRef:
    """A resolved-by-name call site: ``caller`` invokes ``callee_name``."""

    caller_qname: Optional[str]   # enclosing FunctionDef.qualified_name (None = module level)
    callee_name: str              # e.g. "requests.get", "self.repo.save", "createOrder"
    file: str
    line: int
    args: list[ArgHint] = field(default_factory=list)
    caller_class: Optional[str] = None  # enclosing class name, carried by the extractor
                                        # so resolution need not re-derive it by casing


@dataclass
class ImportRef:
    module: str                    # e.g. "payment.client", "requests"
    names: list[str] = field(default_factory=list)   # imported symbols
    alias: Optional[str] = None
    file: str = ""
    line: int = 0


@dataclass
class RouteDef:
    framework: str                 # "fastapi" | "flask" | "spring" | "express" | "gin" ...
    http_method: str               # GET/POST/... or "ANY"
    path: Optional[str]
    handler_qname: Optional[str]   # qualified name of the handler function
    file: str
    line: int


@dataclass
class ResourceRef:
    """A database / external-service touch point discovered heuristically."""

    category: str                  # "database" | "external_service" | "message_queue" | "cache"
    target: str                    # e.g. "OrderDB", "postgres", "redis", "payments-svc"
    operation: Optional[str]       # "read" | "write" | "publish" | "subscribe" | None
    file: str
    line: int
    evidence: str = ""


@dataclass
class ConfigRef:
    key: str                       # referenced config key, e.g. "ORDER_TIMEOUT_SECONDS"
    default: Optional[str] = None
    file: str = ""
    line: int = 0
    evidence: str = ""


@dataclass
class FileArtifact:
    """Everything extracted from a single source file."""

    path: str
    language: str
    module: str                                    # dotted module / package path
    classes: list[ClassDef] = field(default_factory=list)
    functions: list[FunctionDef] = field(default_factory=list)
    calls: list[CallRef] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)
    routes: list[RouteDef] = field(default_factory=list)
    resources: list[ResourceRef] = field(default_factory=list)
    configs: list[ConfigRef] = field(default_factory=list)
    # Service classes detected by Semgrep (framework annotations like @Service, @RestController).
    service_classes: set[str] = field(default_factory=set)
