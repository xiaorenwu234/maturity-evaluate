package com.example;

/**
 * Complex Java class for testing CFG, metrics, and call resolution.
 */
public class OrderService {
    
    private final PaymentService paymentService;
    private final InventoryService inventoryService;
    
    public OrderService(PaymentService paymentService, InventoryService inventoryService) {
        this.paymentService = paymentService;
        this.inventoryService = inventoryService;
    }
    
    /**
     * Complex method with multiple control flow structures.
     */
    public String createOrder(String itemId, double amount, int quantity) {
        // Validation
        if (itemId == null || itemId.isEmpty()) {
            throw new IllegalArgumentException("Item ID cannot be empty");
        }
        
        if (amount <= 0) {
            throw new IllegalArgumentException("Amount must be positive");
        }
        
        // Calculate total
        double total = amount * quantity;
        
        // Apply discount based on quantity
        if (quantity >= 100) {
            total *= 0.9;  // 10% discount
        } else if (quantity >= 50) {
            total *= 0.95; // 5% discount
        }
        
        // Check inventory
        boolean inStock = inventoryService.checkStock(itemId, quantity);
        if (!inStock) {
            return "OUT_OF_STOCK";
        }
        
        // Process payment
        try {
            String transactionId = paymentService.processPayment(total);
            
            // Update inventory
            inventoryService.reserveStock(itemId, quantity);
            
            // Generate order ID
            String orderId = "ORD-" + transactionId + "-" + itemId;
            
            return orderId;
            
        } catch (PaymentException e) {
            System.err.println("Payment failed: " + e.getMessage());
            return "PAYMENT_FAILED";
        } catch (Exception e) {
            System.err.println("Unexpected error: " + e.getMessage());
            return "ERROR";
        } finally {
            System.out.println("Order processing completed for item: " + itemId);
        }
    }
    
    /**
     * Method with loops and nested conditions.
     */
    public void batchProcessOrders(String[] itemIds, double[] amounts) {
        int successCount = 0;
        int failCount = 0;
        
        for (int i = 0; i < itemIds.length; i++) {
            String itemId = itemIds[i];
            double amount = amounts[i];
            
            try {
                String result = createOrder(itemId, amount, 1);
                
                if (result.startsWith("ORD-")) {
                    successCount++;
                } else {
                    failCount++;
                    System.out.println("Failed to process: " + itemId + ", reason: " + result);
                }
                
            } catch (Exception e) {
                failCount++;
                System.err.println("Exception processing " + itemId + ": " + e.getMessage());
            }
        }
        
        System.out.println("Batch complete: " + successCount + " succeeded, " + failCount + " failed");
    }
    
    /**
     * Method with switch statement.
     */
    public String getOrderStatus(String orderId) {
        if (orderId == null) {
            return "INVALID";
        }
        
        // Simulate status lookup
        int hashCode = Math.abs(orderId.hashCode() % 5);
        
        switch (hashCode) {
            case 0:
                return "PENDING";
            case 1:
                return "PROCESSING";
            case 2:
                return "SHIPPED";
            case 3:
                return "DELIVERED";
            case 4:
                return "CANCELLED";
            default:
                return "UNKNOWN";
        }
    }
    
    /**
     * Recursive method.
     */
    public int calculateFactorial(int n) {
        if (n < 0) {
            throw new IllegalArgumentException("Negative number");
        }
        if (n == 0 || n == 1) {
            return 1;
        }
        return n * calculateFactorial(n - 1);
    }
}
