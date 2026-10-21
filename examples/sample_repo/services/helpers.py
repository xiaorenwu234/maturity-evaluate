"""Small helpers for the services package (imported relatively on purpose)."""
from __future__ import annotations


def fmt_order_id(raw: str) -> str:
    """Normalise an order id to the canonical ``ord-xxxx`` display form."""
    return raw if raw.startswith("ord-") else f"ord-{raw}"
