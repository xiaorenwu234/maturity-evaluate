package com.example;

/**
 * Payment service for testing cross-service calls.
 */
public class PaymentService {
    
    public String processPayment(double amount) throws PaymentException {
        if (amount <= 0) {
            throw new PaymentException("Invalid amount");
        }
        
        // Simulate payment processing
        if (amount > 10000) {
            throw new PaymentException("Amount exceeds limit");
        }
        
        return "TXN-" + System.currentTimeMillis();
    }
    
    public void refund(String transactionId) {
        System.out.println("Refunding transaction: " + transactionId);
    }
}
