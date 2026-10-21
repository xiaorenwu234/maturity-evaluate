package com.example;

public class PaymentService {
    public void pay(String orderId, double amount) {
        System.out.println("Paying " + amount + " for order " + orderId);
    }

    public void refund(String orderId) {
        System.out.println("Refunding order " + orderId);
    }
}
