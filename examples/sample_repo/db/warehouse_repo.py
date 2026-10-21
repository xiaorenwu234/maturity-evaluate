"""Warehouse stock persistence (MySQL)."""
from __future__ import annotations

import pymysql


class WarehouseRepository:
    def __init__(self) -> None:
        self.conn = pymysql.connect(host="warehouse-db.internal", db="warehouse")

    def update_stock(self, sku: str, delta: int) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE stock SET qty = qty + %s WHERE sku = %s", (delta, sku))
            return cur.rowcount > 0

    def read_stock(self, sku: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT qty FROM stock WHERE sku = %s", (sku,))
            row = cur.fetchone()
            return row[0] if row else 0
