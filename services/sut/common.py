"""Shared telemetry for every service: OpenTelemetry traces, Prometheus metrics, JSON logs.

Each service is configured only through environment variables, so a "deploy" in the chaos runner is
simply a container restart with a new version and new settings, which is what a CD system does.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable

from fastapi import FastAPI, Request, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = os.environ.get("SERVICE_NAME", "unknown")
VERSION = os.environ.get("APP_VERSION", "0.0.0")


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# ---------------------------------------------------------------- logging
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        doc = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": SERVICE,
            "version": VERSION,
            "msg": record.getMessage(),
        }
        if ctx.is_valid:
            doc["trace_id"] = format(ctx.trace_id, "032x")
        for key in ("route", "status", "duration_ms", "error", "query", "dependency"):
            if hasattr(record, key):
                doc[key] = getattr(record, key)
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(doc)


def get_logger() -> logging.Logger:
    log = logging.getLogger(SERVICE)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


log = get_logger()

# ---------------------------------------------------------------- tracing
provider = TracerProvider(
    resource=Resource.create({"service.name": SERVICE, "service.version": VERSION})
)
provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(
            endpoint=os.environ.get("OTEL_ENDPOINT", "http://otel-collector:4318") + "/v1/traces"
        )
    )
)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(SERVICE)
HTTPXClientInstrumentor().instrument()

# ---------------------------------------------------------------- metrics
REQUESTS = Counter("http_requests_total", "HTTP requests", ["route", "method", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
DEPENDENCY_LATENCY = Histogram(
    "dependency_duration_seconds",
    "Latency of calls to databases, caches and other services",
    ["dependency", "operation"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
DEPENDENCY_ERRORS = Counter(
    "dependency_errors_total", "Failed calls to dependencies", ["dependency", "operation", "error"]
)


def timed_dependency(dependency: str, operation: str) -> Callable:
    """Decorator-free context manager: records latency, errors and a span for a dependency call."""

    class _Timer:
        def __enter__(self):
            self.start = time.perf_counter()
            self.span = tracer.start_as_current_span(
                f"{dependency} {operation}",
                attributes={"peer.service": dependency, "db.operation": operation},
            )
            self.span.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            elapsed = time.perf_counter() - self.start
            DEPENDENCY_LATENCY.labels(dependency, operation).observe(elapsed)
            if exc_type is not None:
                DEPENDENCY_ERRORS.labels(dependency, operation, exc_type.__name__).inc()
                trace.get_current_span().set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            self.span.__exit__(exc_type, exc, tb)
            return False

    return _Timer()


def create_app() -> FastAPI:
    app = FastAPI(title=SERVICE)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,health,__chaos")

    @app.middleware("http")
    async def observe(request: Request, call_next):
        if request.url.path in ("/metrics", "/health") or request.url.path.startswith("/__chaos"):
            return await call_next(request)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path)
            elapsed = time.perf_counter() - start
            REQUESTS.labels(path, request.method, str(status)).inc()
            LATENCY.labels(path).observe(elapsed)
            if status >= 500:
                log.error(
                    "request failed",
                    extra={"route": path, "status": status, "duration_ms": round(elapsed * 1000)},
                )

    # async: served on the event loop, so metrics stay available even when worker threads are busy
    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    async def health() -> dict:
        return {"service": SERVICE, "version": VERSION}

    @app.on_event("startup")
    def announce() -> None:
        log.info(f"{SERVICE} {VERSION} started")

    return app
