"""Payment service -- charges/refunds via an external gateway."""
from __future__ import annotations

import os

import httpx

PAYMENT_GATEWAY_URL = os.getenv("PAYMENT_GATEWAY_URL", "https://stripe.internal/v1")


class PaymentService:
    def __init__(self) -> None:
        self.base_url = PAYMENT_GATEWAY_URL

    def pay(self, amount: float) -> dict:
        resp = httpx.post(f"{self.base_url}/charges", json={"amount": amount})
        resp.raise_for_status()
        return resp.json()

    def refund(self, charge_id: str) -> dict:
        resp = httpx.post(f"{self.base_url}/refunds", json={"charge_id": charge_id})
        return resp.json()
