# TraceWatch Deployment

Deployment platform: Render

## Services

- Auth Service
- Payment Service
- Notification Service
- Order Service
- Batch Indexer
- OTel Collector
- Jaeger
- API Gateway
- Frontend
- Prometheus
- Grafana

## Deployment order

1. Auth Service
2. Payment Service
3. Notification Service
4. Order Service
5. Batch Indexer
6. OTel Collector
7. Jaeger
8. API Gateway
9. Frontend
10. Prometheus
11. Grafana

All inter-service URLs must use deployed Render URLs,
not Docker service names or localhost.