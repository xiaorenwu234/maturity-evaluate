package com.example;

/**
 * Custom exception for payment failures.
 */
public class PaymentException extends Exception {
    public PaymentException(String message) {
        super(message);
    }
}
