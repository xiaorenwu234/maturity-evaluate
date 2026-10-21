"""Framework & resource driven by Semgrep.

Replaces the previous in-house regex / keyword heuristics with rules declared
as Semgrep YAML under ``semgrep_rules/``. The public entry point is
:func:`enrich_all`, which the code collector calls **once per repo** (Semgrep
is a batch tool; per-file invocation would be far slower).

Contract preserved (see :mod:`kg_builder.collectors.code.artifact`):

* ``RouteDef``     -- HTTP endpoints (FastAPI / Flask / Spring / Express / Gin)
* ``ResourceRef``  -- databases, caches, message queues, external services
* ``ConfigRef``    -- configuration keys read by the code

Design principles:

* **No silent fallback**: if Semgrep is not installed, we skip enrichment and
  surface a warning -- the same philosophy already applied to non-Python
  tree-sitter parsers.
* **Conservative**: rules are syntactically anchored; whatever they cannot
  confidently classify stays as a plain call / import.
* **De-duplicated per artifact**: the same DB / cache / external service can be
  touched from many call sites -- we keep one ResourceRef per logical target.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .artifact import (
    ConfigRef,
    FileArtifact,
    ResourceRef,
    RouteDef,
)
from .semgrep_runner import (
    RULES_DIR,
    Finding,
    SemgrepError,
    scan_repo,
    semgrep_status,
)

# Post-processing on individual *values* (not raw source): these small regexes
# let us pull a concrete DB / host name out of a captured URL string.
_DB_URL_RE = re.compile(
    r"(?:postgres(?:ql)?|mysql|sqlite|mongodb(?:\+srv)?|redis)(?:ql)?://"
    r"[^/\s\"']*/([A-Za-z0-9_\-]+)",
    re.I,
)
_HOST_RE = re.compile(r"https?://([A-Za-z0-9._\-]+)", re.I)


def enrich_all(artifacts: Iterable[FileArtifact], repo_path: Path) -> dict:
    """Run Semgrep once over ``repo_path`` and attach findings to artifacts.

    Returns a status dict shaped like::

        {
            "ok": bool,                  # False when the CLI is missing / errored
            "engine": str,               # version banner or error text
            "findings": int,             # total Semgrep matches consumed
            "applied": {                 # per-kind counters (for metrics/tests)
                "routes": int, "resources": int, "configs": int,
            },
            "warnings": [str, ...],
        }
    """
    artifacts = list(artifacts)
    ok, msg = semgrep_status()
    status = {
        "ok": ok,
        "engine": msg,
        "findings": 0,
        "applied": {"routes": 0, "resources": 0, "configs": 0, "services": 0},
        "warnings": [],
    }
    if not ok:
        status["warnings"].append(
            f"semgrep unavailable ({msg}); skipping framework/resource/config enrichment"
        )
        return status

    try:
        findings = scan_repo(repo_path, RULES_DIR)
    except SemgrepError as exc:
        status["ok"] = False
        status["warnings"].append(f"semgrep scan failed: {exc}")
        return status

    status["findings"] = len(findings)

    # Semgrep emits paths relative to ``repo_path`` (we run with cwd=repo). The
    # collector stores the same relative paths on FileArtifact -- build a direct
    # lookup. Files that only appear in Semgrep output (e.g. skipped by the AST
    # extractor for size / no-parser) are dropped silently.
    by_path: dict[str, FileArtifact] = {a.path: a for a in artifacts}
    for f in findings:
        art = by_path.get(f.path) or by_path.get(f.path.lstrip("./"))
        if art is None:
            continue
        applied = _apply(art, f)
        if applied:
            status["applied"][applied] += 1
    return status


def _apply(art: FileArtifact, f: Finding) -> str | None:
    """Map a single finding onto ``art``. Returns the counter key or ``None``."""
    kind = f.kind
    if kind == "route":
        return _apply_route(art, f)
    if kind == "resource":
        return _apply_resource(art, f)
    if kind == "config":
        return _apply_config(art, f)
    if kind == "service":
        return _apply_service(art, f)
    return None


# --------------------------------------------------------------------------- routes
def _apply_route(art: FileArtifact, f: Finding) -> str | None:
    p = f.payload
    method = str(p.get("m") or "ANY").upper()
    raw_path = p.get("p")
    path = _as_str_or_none(raw_path)
    handler = _as_str_or_none(p.get("h"))
    framework = str(p.get("fw") or "unknown")
    # Skip pure duplicates (same method + path already recorded).
    for r in art.routes:
        if r.http_method == method and r.path == path:
            return None
    handler_qname = f"{art.module}.{handler}" if handler and art.module else (handler or None)
    art.routes.append(
        RouteDef(
            framework=framework,
            http_method=method,
            path=path,
            handler_qname=handler_qname,
            file=art.path,
            line=f.line,
        )
    )
    return "routes"


# ------------------------------------------------------------------------ resources
def _apply_resource(art: FileArtifact, f: Finding) -> str | None:
    p = f.payload
    category = str(p.get("cat") or "external_service")
    operation = _as_str_or_none(p.get("op"))
    target = _resolve_target(p, category)
    if not target:
        return None
    evidence = f"{f.rule_id} {p}".strip()
    # Same (category, target) may be reported by several rules; merge them so we
    # don't end up with both a generic DEPENDS_ON and a specific WRITES edge:
    #   * exact duplicate            -> drop
    #   * new has op, old op-less    -> upgrade in place
    #   * new is op-less, old has op -> drop new
    #   * different explicit ops     -> keep both (read + write on same DB)
    for existing in art.resources:
        if existing.category != category or existing.target != target:
            continue
        if existing.operation == operation:
            return None
        if existing.operation is None and operation is not None:
            existing.operation = operation
            existing.evidence = evidence
            return None
        if operation is None and existing.operation is not None:
            return None
    art.resources.append(
        ResourceRef(
            category=category,
            target=target,
            operation=operation,
            file=art.path,
            line=f.line,
            evidence=evidence,
        )
    )
    return "resources"


def _resolve_target(payload: dict, category: str) -> str | None:
    """Pick the best concrete target for a resource finding."""
    # 1. Explicit URL captured from the rule ($URL / $DEFAULT string literal).
    raw_url = payload.get("u")
    if isinstance(raw_url, str) and raw_url:
        if category == "database":
            m = _DB_URL_RE.search(raw_url)
            if m:
                return m.group(1)
        host = _HOST_RE.search(raw_url)
        if host:
            return host.group(1)
    # 2. Rule-declared name (e.g. "sqlalchemy", "httpx", "order_repo").
    name = payload.get("name")
    if isinstance(name, str) and name:
        return name
    # 3. Fallback per-category default so we still create a node.
    return {
        "database": "database",
        "cache": "cache",
        "message_queue": "message_queue",
        "external_service": "external_service",
    }.get(category)


# ---------------------------------------------------------------------------- configs
def _apply_config(art: FileArtifact, f: Finding) -> str | None:
    p = f.payload
    key = _as_str_or_none(p.get("key"))
    if not key:
        return None
    default = _as_str_or_none(p.get("def"))
    # Spring @Value arrives wrapped: ``${key}`` / ``${key:default}`` -- unwrap
    # and lift any embedded default into the dedicated slot.
    if key.startswith("${") and key.endswith("}"):
        inner = key[2:-1]
        if ":" in inner:
            inner, embedded = inner.split(":", 1)
            default = default or (embedded.strip() or None)
        key = inner.strip()
    if not key:
        return None
    for existing in art.configs:
        if existing.key == key:
            # Merge: upgrade default if we now know one.
            if default and not existing.default:
                existing.default = default
            return None
    art.configs.append(
        ConfigRef(
            key=key,
            default=default,
            file=art.path,
            line=f.line,
            evidence=f"{p.get('src', 'env')}:{key}",
        )
    )
    return "configs"


# --------------------------------------------------------------------------- services
def _apply_service(art: FileArtifact, f: Finding) -> str | None:
    """Record a service class detected by Semgrep (e.g., @Service, @RestController)."""
    p = f.payload
    class_name = _as_str_or_none(p.get("class"))
    if not class_name:
        return None
    # Add to the artifact's service_classes set.
    art.service_classes.add(class_name)
    return "services"


# ------------------------------------------------------------------------------ utils
def _as_str_or_none(value) -> str | None:
    """Coerce a Semgrep-payload value into a plain string (``None`` if empty)."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    return str(value)
