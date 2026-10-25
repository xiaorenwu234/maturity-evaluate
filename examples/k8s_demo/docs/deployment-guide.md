# 部署指南

## 前置条件

- Kubernetes 集群 (v1.24+)
- kubectl 已配置 kubeconfig

## 部署步骤

### 1. 创建 Namespace

```bash
kubectl apply -f k8s/namespace.yaml
```

### 2. 部署服务

```bash
kubectl apply -f k8s/order-service.yaml
kubectl apply -f k8s/inventory-service.yaml
kubectl apply -f k8s/payment-service.yaml
```

### 3. 验证

```bash
kubectl get pods -n kgb-demo
kubectl get svc -n kgb-demo
```

## 服务端口映射

| 服务 | 容器端口 | Service 端口 |
|------|---------|-------------|
| order-service | 8080 | 80 |
| inventory-service | 8080 | 80 |
| payment-service | 8080 | 80 |
