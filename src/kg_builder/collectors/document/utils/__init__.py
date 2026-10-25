"""Config parser registry for document collector.

All domain-specific parsers are registered here. The orchestrator calls
:func:`find_parser` to locate the right parser for a given config file.

Parsers are checked in order -- more specific parsers should come before
catch-all ones (e.g. K8sParser before AppConfigParser).
"""
from __future__ import annotations

from typing import Optional

from .base import BaseConfigParser, ParseResult
from .k8s_parser import K8sParser
from .docker_parser import DockerParser
from .monitor_parser import MonitorParser
from .ci_parser import CIParser
from .api_spec_parser import ApiSpecParser
from .iac_parser import IacParser
from .app_config_parser import AppConfigParser

# Ordered registry: more specific parsers first, catch-all last.
PARSER_REGISTRY: list[type[BaseConfigParser]] = [
    K8sParser,          # apiVersion + kind header
    DockerParser,       # Dockerfile / docker-compose
    MonitorParser,      # prometheus / grafana
    CIParser,           # GitHub Actions / GitLab CI / Jenkins
    ApiSpecParser,      # OpenAPI / Swagger
    IacParser,          # Terraform / Ansible
    AppConfigParser,    # catch-all for remaining structured configs
]

# Category -> parser class mapping for LLM-routed dispatch
_CATEGORY_MAP: dict[str, type[BaseConfigParser]] = {
    cls.category: cls for cls in PARSER_REGISTRY if cls.category
}


def find_parser(rel_path: str, ext: str, content: str,
                category: Optional[str] = None) -> Optional[BaseConfigParser]:
    """Find the appropriate parser for a config file.

    If *category* is provided (from LLM classification), try that parser first.
    Then fall back to walking the registry with ``can_parse()``.

    Returns a parser instance or None if no parser matches.
    """
    # Try LLM-suggested category first
    if category and category in _CATEGORY_MAP:
        parser_cls = _CATEGORY_MAP[category]
        parser = parser_cls()
        if parser.can_parse(rel_path, ext, content):
            return parser

    # Walk registry
    for parser_cls in PARSER_REGISTRY:
        parser = parser_cls()
        if parser.can_parse(rel_path, ext, content):
            return parser

    return None


__all__ = [
    "BaseConfigParser",
    "ParseResult",
    "PARSER_REGISTRY",
    "find_parser",
]
