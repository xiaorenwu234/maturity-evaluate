"""Order service - handles order creation and management."""


class OrderService:
    """Manages customer orders."""

    def __init__(self, inventory_service, payment_service):
        self.inventory_service = inventory_service
        self.payment_service = payment_service

    def create_order(self, product_id: str, quantity: int) -> dict:
        """Create a new order: check stock, process payment, return order info."""
        stock = self.inventory_service.check_stock(product_id)
        if stock < quantity:
            raise ValueError(f"Insufficient stock for {product_id}")

        self.inventory_service.reserve(product_id, quantity)
        payment_result = self.payment_service.process_payment(quantity * 10.0)

        return {
            "order_id": f"ORD-{product_id}-{quantity}",
            "status": "confirmed",
            "payment": payment_result,
        }

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an existing order and release reserved stock."""
        self.inventory_service.release(order_id)
        self.payment_service.refund(order_id)
        return True
