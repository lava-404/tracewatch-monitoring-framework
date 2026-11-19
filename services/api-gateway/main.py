# services/api-gateway/main.py
import os
import re
import time
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# OpenTelemetry imports
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.propagate import inject, extract
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry import propagate

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8001")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8002")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8003")
NOTIFICATION_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://notification-service:8004")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
JAEGER_QUERY_URL = os.getenv("JAEGER_QUERY_URL", "http://localhost:16686")

# Setup OpenTelemetry
def setup_telemetry():
    resource = Resource.create({
        "service.name": "api-gateway",
        "service.version": "1.0.0",
        "deployment.environment": "production"
    })
    provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("api-gateway")

tracer = setup_telemetry()

# Prometheus metrics
REQUEST_COUNT = Counter(
    'api_gateway_requests_total',
    'Total API Gateway requests',
    ['method', 'endpoint', 'status_code', 'service']
)
REQUEST_LATENCY = Histogram(
    'api_gateway_request_duration_seconds',
    'API Gateway request duration',
    ['method', 'endpoint', 'service']
)
ERROR_COUNT = Counter(
    'api_gateway_errors_total',
    'Total API Gateway errors',
    ['service', 'error_type']
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("API Gateway starting up...")
    yield
    logger.info("API Gateway shutting down...")

app = FastAPI(title="TraceWatch API Gateway", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_headers_with_trace():
    """Inject trace context into headers for downstream propagation."""
    headers = {}
    inject(headers)
    return headers

async def proxy_request(method: str, url: str, service_name: str, **kwargs):
    """Proxy an HTTP request to a downstream service with trace propagation."""
    start_time = time.time()
    headers = get_headers_with_trace()
    if 'headers' in kwargs:
        kwargs['headers'].update(headers)
    else:
        kwargs['headers'] = headers
    
    with tracer.start_as_current_span(f"{service_name}.{method}") as span:
        span.set_attribute("service.target", service_name)
        span.set_attribute("http.url", url)
        span.set_attribute("http.method", method)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await getattr(client, method.lower())(url, **kwargs)
            duration = time.time() - start_time
            span.set_attribute("http.status_code", response.status_code)
            REQUEST_COUNT.labels(method=method, endpoint=url, status_code=response.status_code, service=service_name).inc()
            REQUEST_LATENCY.labels(method=method, endpoint=url, service=service_name).observe(duration)
            return response
        except Exception as e:
            duration = time.time() - start_time
            span.record_exception(e)
            span.set_status(trace.StatusCode.ERROR, str(e))
            ERROR_COUNT.labels(service=service_name, error_type=type(e).__name__).inc()
            REQUEST_COUNT.labels(method=method, endpoint=url, status_code=503, service=service_name).inc()
            raise HTTPException(status_code=503, detail=f"Service {service_name} unavailable: {str(e)}")

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "api-gateway", "timestamp": time.time()}

@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.post("/api/auth/register")
async def register(request: Request):
    with tracer.start_as_current_span("api-gateway.auth.register") as span:
        span.set_attribute("endpoint", "/api/auth/register")
        body = await request.json()
        response = await proxy_request("POST", f"{AUTH_SERVICE_URL}/register", "auth-service", json=body)
        return Response(content=response.content, status_code=response.status_code, media_type="application/json")

@app.post("/api/auth/login")
async def login(request: Request):
    with tracer.start_as_current_span("api-gateway.auth.login") as span:
        span.set_attribute("endpoint", "/api/auth/login")
        body = await request.json()
        response = await proxy_request("POST", f"{AUTH_SERVICE_URL}/login", "auth-service", json=body)
        return Response(content=response.content, status_code=response.status_code, media_type="application/json")

@app.post("/api/orders")
async def create_order(request: Request):
    with tracer.start_as_current_span("api-gateway.orders.create") as span:
        span.set_attribute("endpoint", "/api/orders")
        body = await request.json()
        response = await proxy_request("POST", f"{ORDER_SERVICE_URL}/orders", "order-service", json=body)
        return Response(content=response.content, status_code=response.status_code, media_type="application/json")

@app.get("/api/orders/{order_id}")
async def get_order(order_id: str):
    with tracer.start_as_current_span("api-gateway.orders.get") as span:
        span.set_attribute("order.id", order_id)
        response = await proxy_request("GET", f"{ORDER_SERVICE_URL}/orders/{order_id}", "order-service")
        return Response(content=response.content, status_code=response.status_code, media_type="application/json")

@app.post("/api/payments/process")
async def process_payment(request: Request):
    with tracer.start_as_current_span("api-gateway.payments.process") as span:
        span.set_attribute("endpoint", "/api/payments/process")
        body = await request.json()
        response = await proxy_request("POST", f"{PAYMENT_SERVICE_URL}/payments/process", "payment-service", json=body)
        return Response(content=response.content, status_code=response.status_code, media_type="application/json")

@app.post("/api/notifications/send")
async def send_notification(request: Request):
    with tracer.start_as_current_span("api-gateway.notifications.send") as span:
        span.set_attribute("endpoint", "/api/notifications/send")
        body = await request.json()
        response = await proxy_request("POST", f"{NOTIFICATION_SERVICE_URL}/notifications/send", "notification-service", json=body)
        return Response(content=response.content, status_code=response.status_code, media_type="application/json")


@app.post("/api/simulate/payment-failure")
async def simulate_payment_failure(request: Request):
    body = await request.json()
    response = await proxy_request(
        "POST",
        f"{PAYMENT_SERVICE_URL}/simulate/failure",
        "payment-service",
        json=body
    )
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type="application/json"
    )


@app.post("/api/simulate/notification-failure")
async def simulate_notification_failure(request: Request):
    body = await request.json()
    response = await proxy_request(
        "POST",
        f"{NOTIFICATION_SERVICE_URL}/simulate/failure",
        "notification-service",
        json=body
    )
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type="application/json"
    )

@app.post("/api/simulate/order-flow")
async def simulate_order_flow(request: Request):
    """Run a complete order flow through Order Service orchestration."""
    with tracer.start_as_current_span("api-gateway.simulate.order-flow") as span:
        body = await request.json()
        scenario = body.get("scenario", "complete-payment-flow")

        span.set_attribute("execution.scenario", scenario)
        span.set_attribute("execution.simulate_payment_failure", bool(body.get("simulate_payment_failure", False)))
        span.set_attribute("execution.simulate_notification_failure", bool(body.get("simulate_notification_failure", False)))

        order_payload = {
            "user_id": body.get("user_id", "user-123"),
            "items": body.get("items", [{"product": "Widget", "quantity": 1, "price": 29.99}]),
            "total": body.get("total", 29.99),
            "simulate_payment_failure": body.get("simulate_payment_failure", False),
            "simulate_notification_failure": body.get("simulate_notification_failure", False),
        }

        # The Order Service owns the orchestration. Do not call Payment or
        # Notification directly here, otherwise those operations run twice.
        try:
            order_resp = await proxy_request(
                "POST",
                f"{ORDER_SERVICE_URL}/orders",
                "order-service",
                json=order_payload,
            )
            order_data = order_resp.json()
            span.set_attribute("order.id", order_data.get("order_id", "unknown"))
            span.set_attribute("execution.result", order_data.get("status", "unknown"))

            trace_id = format(span.get_span_context().trace_id, "032x")
            return JSONResponse(
                content={
                    "flow": "complete",
                    "scenario": scenario,
                    "trace_id": trace_id,
                    "results": {"order": order_data},
                },
                status_code=order_resp.status_code,
            )
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.StatusCode.ERROR, str(e))
            trace_id = format(span.get_span_context().trace_id, "032x")
            return JSONResponse(
                content={
                    "flow": "failed",
                    "scenario": scenario,
                    "trace_id": trace_id,
                    "results": {"order": {"error": str(e)}},
                },
                status_code=503,
            )

@app.get("/api/observability/traces/{trace_id}")
async def get_jaeger_trace(trace_id: str):
    """Fetch a real trace from Jaeger Query API."""
    if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id):
        raise HTTPException(status_code=400, detail="Invalid trace ID")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(f"{JAEGER_QUERY_URL}/api/traces/{trace_id}")
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Trace not found in Jaeger yet")
        response.raise_for_status()
        return response.json()
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.exception("Unable to query Jaeger")
        raise HTTPException(status_code=502, detail=f"Jaeger query failed: {exc}")


@app.get("/api/services/health")
async def all_services_health():
    """Check health of all downstream services."""
    services = {
        "auth-service": f"{AUTH_SERVICE_URL}/health",
        "order-service": f"{ORDER_SERVICE_URL}/health",
        "payment-service": f"{PAYMENT_SERVICE_URL}/health",
        "notification-service": f"{NOTIFICATION_SERVICE_URL}/health",
    }
    health_results = {}

    async with httpx.AsyncClient(timeout=5.0) as client:
        for svc, url in services.items():
            try:
                r = await client.get(url)
                health_results[svc] = {
                    "status": "healthy" if r.status_code == 200 else "unhealthy",
                    "code": r.status_code
                }
            except Exception as e:
                health_results[svc] = {
                    "status": "unreachable",
                    "error": str(e)
                }

    health_results["api-gateway"] = {"status": "healthy", "code": 200}
    return health_results