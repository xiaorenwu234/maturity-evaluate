"""Inventory service -- reserves stock in the warehouse database."""
from __future__ import annotations

from ..db.warehouse_repo import WarehouseRepository  # .. relative import


class InventoryService:
    def __init__(self) -> None:
        self.warehouse = WarehouseRepository()

    def reserve(self, sku: str, qty: int) -> bool:
        return self.warehouse.update_stock(sku, -qty)

    def available(self, sku: str) -> int:
        return self.warehouse.read_stock(sku)
