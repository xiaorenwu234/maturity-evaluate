package com.example;

public class OrderService {
    private final PaymentService paymentService = new PaymentService();

    public String createOrder(String itemId, double amount) {
        String orderId = "ORD-" + itemId;
        paymentService.pay(orderId, amount);
        return orderId;
    }

    public void cancelOrder(String orderId) {
        paymentService.refund(orderId);
    }
}
