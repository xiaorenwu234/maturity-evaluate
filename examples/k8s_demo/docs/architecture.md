# 系统架构文档

## 概述

本系统是一个电商订单管理平台，采用微服务架构部署在 Kubernetes 集群中。

## 服务清单

| 服务名称 | K8s Deployment | K8s Namespace | 源码文件 | 职责 |
|---------|---------------|---------------|---------|------|
| OrderService | order-service | kgb-demo | src/order_service.py | 订单创建与管理 |
| InventoryService | inventory-service | kgb-demo | src/inventory_service.py | 库存查询与扣减 |
| PaymentService | payment-service | kgb-demo | src/payment_service.py | 支付处理与退款 |

## 服务调用关系

- **OrderService** 调用 **InventoryService** 检查库存和预留商品
- **OrderService** 调用 **PaymentService** 处理支付
- **InventoryService** 独立运行，不依赖其他服务
- **PaymentService** 通过 HTTPS 连接外部支付网关

## 部署说明

所有服务部署在 `kgb-demo` namespace 下，每个服务对应一个 Deployment 和一个 ClusterIP Service。

### 资源配置

- order-service: 2 replicas, CPU 200m, Memory 256Mi
- inventory-service: 1 replica, CPU 100m, Memory 128Mi
- payment-service: 2 replicas, CPU 200m, Memory 256Mi
# v4: update architecture
