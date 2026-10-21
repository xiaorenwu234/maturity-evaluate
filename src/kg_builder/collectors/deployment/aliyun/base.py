"""Shared utilities for Alibaba Cloud collectors.

Provides credential creation, paginated API helpers, and common error handling
so individual collectors stay focused on product-specific logic.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Callable, Optional, TypeVar

from ...config import DeploymentSettings

logger = logging.getLogger(__name__)

T = TypeVar("T")


def get_credentials(settings: DeploymentSettings):
    """Create an ``alibabacloud_credentials.Client`` from settings or env vars.

    Precedence: explicit config fields > ``ALIBABA_CLOUD_*`` environment variables.
    Raises ``ImportError`` if the SDK is not installed.
    """
    try:
        from alibabacloud_credentials.client import Client as CredentialClient
    except ImportError as exc:
        raise ImportError(
            "alibabacloud-credentials is required for deployment collector. "
            "Install with: pip install -e '.[alicloud]'"
        ) from exc

    ak = settings.aliyun_access_key_id or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
    sk = settings.aliyun_access_key_secret or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")

    if ak and sk:
        return CredentialClient(
            type="access_key",
            access_key_id=ak,
            access_key_secret=sk,
        )
    # Fall back to default credential chain (env / instance role / OIDC)
    return CredentialClient()


def resolve_region(settings: DeploymentSettings, override: Optional[str] = None) -> str:
    """Return the effective region: per-product override > top-level setting."""
    return override or settings.aliyun_region


def parse_time(raw: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 timestamps returned by Alibaba Cloud APIs."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def paginate(
    fetch_page: Callable[[int, int], tuple[list[T], int]],
    page_size: int = 100,
) -> list[T]:
    """Generic paginator for Alibaba Cloud list-APIs.

    ``fetch_page(page_no, page_size)`` must return ``(items, total_count)``.
    Iterates until all items are collected.
    """
    all_items: list[T] = []
    page_no = 1
    while True:
        items, total = fetch_page(page_no, page_size)
        all_items.extend(items)
        if len(all_items) >= total or not items:
            break
        page_no += 1
    return all_items
