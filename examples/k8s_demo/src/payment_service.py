"""Payment service - processes payments and refunds."""


class PaymentService:
    """Handles payment transactions."""

    def __init__(self, gateway_url: str = "https://pay.example.com"):
        self.gateway_url = gateway_url

    def process_payment(self, amount: float) -> dict:
        """Process a payment transaction."""
        return {"transaction_id": f"TXN-{amount}", "status": "success"}

    def refund(self, order_id: str) -> bool:
        """Refund a completed order."""
        return True
# v3: payment retry logic
