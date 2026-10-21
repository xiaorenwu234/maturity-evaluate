"""Order persistence layer backed by the orders database."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ENGINE = create_engine("postgresql://db.internal/orders")
Session = sessionmaker(bind=ENGINE)


class OrderRepository:
    def save(self, payload: dict) -> dict:
        session = Session()
        session.add(payload)
        session.commit()
        return {"id": "ord-1", **payload}

    def find_by_id(self, order_id: str):
        session = Session()
        return session.query("orders").filter_by(id=order_id).first()

    def delete(self, order_id: str) -> None:
        session = Session()
        session.delete({"id": order_id})
        session.commit()
