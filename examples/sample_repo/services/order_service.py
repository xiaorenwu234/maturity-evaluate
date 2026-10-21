"""Order service -- the entry point of the checkout business chain.

Relative imports below are deliberate: they exercise the resolver's
single-dot (same package) and double-dot (climb to repo root) handling.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from ..db.order_repo import OrderRepository          # .. climbs to the repo root
from .payment_service import PaymentService          # . same package
from .inventory_service import InventoryService
from .helpers import fmt_order_id

ORDER_TIMEOUT_SECONDS = int(os.getenv("ORDER_TIMEOUT_SECONDS", "5"))


class OrderService:
    def __init__(self) -> None:
        self.order_repo = OrderRepository()
        self.payment_service = PaymentService()
        self.inventory = InventoryService()

    def create_order(self, payload: dict) -> dict:
        order = self.order_repo.save(payload)
        # Reserve stock, then take payment -- core business link.
        self.inventory.reserve(order["sku"], order["qty"])
        self.payment_service.pay(order["amount"])
        return {"order_id": fmt_order_id(order["id"]), "status": "created"}

    def get_order(self, order_id: str) -> dict:
        order = self.order_repo.find_by_id(order_id)
        if order is None:
            # Swallows the miss and returns a stub -- semantically ambiguous on purpose.
            return {"order_id": order_id, "status": "unknown"}
        return order

    def cancel_order(self, order_id: str) -> dict:
        self.order_repo.delete(order_id)
        self.payment_service.refund(order_id)
        return {"order_id": order_id, "status": "cancelled"}


router = APIRouter()
service = OrderService()


@router.post("/order/create")
def _create(payload: dict):
    try:
        return service.create_order(payload)
    except Exception:
        raise HTTPException(status_code=500, detail="create failed")


@router.get("/order/{order_id}")
def _get(order_id: str):
    return service.get_order(order_id)
