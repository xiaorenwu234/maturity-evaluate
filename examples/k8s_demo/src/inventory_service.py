"""Inventory service - manages product stock levels."""


class InventoryService:
    """Tracks and manages product inventory."""

    def __init__(self):
        self._stock = {}

    def check_stock(self, product_id: str) -> int:
        """Return current stock level for a product."""
        return self._stock.get(product_id, 0)

    def reserve(self, product_id: str, quantity: int) -> bool:
        """Reserve stock for an order."""
        current = self.check_stock(product_id)
        if current >= quantity:
            self._stock[product_id] = current - quantity
            return True
        return False

    def release(self, order_id: str) -> None:
        """Release reserved stock when an order is cancelled."""
        pass  # simplified

    def update_stock(self, product_id: str, quantity: int) -> None:
        """Add incoming stock for a product."""
        self._stock[product_id] = self._stock.get(product_id, 0) + quantity
# v2: optimized stock check
