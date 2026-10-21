"""Alibaba Cloud collector sub-package.

Each module exposes a ``collect_xxx(settings) -> (list[KGNode], list[KGEdge])``
function that the parent ``DeploymentCollector`` calls when the corresponding
cloud product is enabled in config.
"""
