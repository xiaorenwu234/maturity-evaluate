"""Deployment collector package (Kubernetes / cloud)."""
from __future__ import annotations

from .k8s_collector import DeploymentCollector

__all__ = ["DeploymentCollector"]
