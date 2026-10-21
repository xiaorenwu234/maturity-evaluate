package com.example;

/**
 * Inventory service for testing cross-service calls.
 */
public class InventoryService {
    
    public boolean checkStock(String itemId, int quantity) {
        // Simulate stock check
        return quantity <= 100;
    }
    
    public void reserveStock(String itemId, int quantity) {
        System.out.println("Reserved " + quantity + " units of " + itemId);
    }
    
    public void releaseStock(String itemId, int quantity) {
        System.out.println("Released " + quantity + " units of " + itemId);
    }
}
