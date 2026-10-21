# Checkout System Architecture

This document describes the ordering domain.

## Services

- **OrderService** is the entry point of the checkout flow. It exposes
  `POST /order/create` and calls `PaymentService` and `InventoryService`.
- **PaymentService** performs charging and refunds through an external gateway.
- **InventoryService** reserves stock in the warehouse.

## Data

- `OrderService` reads and writes the `orders` database.
- `InventoryService` writes the `warehouse` database.

## Interfaces

- `POST /order/create`  -> creates an order (core business link)
- `GET /order/{order_id}` -> query an order
