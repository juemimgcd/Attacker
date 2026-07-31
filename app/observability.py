from __future__ import annotations

import os
import re
from contextvars import ContextVar
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import Request
from loguru import logger
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from conf.settings import ObservabilitySettings

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
request_id_context: ContextVar[str] = ContextVar("attacker_request_id", default="")

HTTP_REQUESTS = Counter(
    "attacker_http_requests_total",
    "Completed HTTP requests.",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "attacker_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
)
HTTP_IN_PROGRESS = Gauge(
    "attacker_http_requests_in_progress",
    "HTTP requests currently being processed.",
    multiprocess_mode="livesum",
)
READINESS = Gauge(
    "attacker_readiness",
    "Dependency readiness where 1 is ready.",
    ["dependency"],
    multiprocess_mode="livemostrecent",
)
EQUIPMENT_EVENTS = Counter(
    "attacker_equipment_events_total",
    "Equipment runtime counters.",
    ["name"],
)
EQUIPMENT_INVALID_PACKAGES = Gauge(
    "attacker_equipment_invalid_packages",
    "Current equipment packages that failed catalog validation.",
    multiprocess_mode="livemostrecent",
)
EQUIPMENT_DURATION = Histogram(
    "attacker_equipment_duration_seconds",
    "Equipment runtime duration.",
    ["name"],
)
JOB_EVENTS = Counter(
    "attacker_job_events_total",
    "Durable job lifecycle events.",
    ["event"],
)
JOB_STATE = Gauge(
    "attacker_jobs",
    "Current durable jobs by lifecycle status.",
    ["status"],
    multiprocess_mode="livemostrecent",
)
JOB_OLDEST_READY_AGE = Gauge(
    "attacker_job_oldest_ready_age_seconds",
    "Age of the oldest queued or retry-ready durable job.",
    multiprocess_mode="livemostrecent",
)
JOB_EXPIRED_LEASES = Gauge(
    "attacker_job_expired_leases",
    "Current jobs whose active worker lease has expired.",
    multiprocess_mode="livemostrecent",
)
WORKER_STALE = Gauge(
    "attacker_worker_stale",
    "Current non-draining workers whose heartbeat is stale.",
    multiprocess_mode="livemostrecent",
)
METRICS_REFRESH_READY = Gauge(
    "attacker_metrics_refresh_ready",
    "Whether repository-backed operational metrics refreshed successfully.",
    multiprocess_mode="livemostrecent",
)

JOB_STATUSES = (
    "queued",
    "leased",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
)
RULE_BOUND_EQUIPMENT_EVENTS = (
    "provider_error",
    "cleanup_failure",
    "package_checksum_mismatch",
    "sandbox_termination",
)
RULE_BOUND_JOB_EVENTS = ("failed", "succeeded")

for _equipment_event in RULE_BOUND_EQUIPMENT_EVENTS:
    EQUIPMENT_EVENTS.labels(name=_equipment_event)
for _job_event in RULE_BOUND_JOB_EVENTS:
    JOB_EVENTS.labels(event=_job_event)

_tracer_provider: TracerProvider | None = None


class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, config: ObservabilitySettings) -> None:
        super().__init__(app)
        self.config = config
        self.tracer = trace.get_tracer("attacker.http")

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        supplied = request.headers.get(self.config.request_id_header, "")
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid4())
        token = request_id_context.set(request_id)
        method = request.method
        started = perf_counter()
        HTTP_IN_PROGRESS.inc()
        response: Response | None = None
        try:
            with (
                logger.contextualize(request_id=request_id),
                self.tracer.start_as_current_span(f"{method} HTTP request") as span,
            ):
                span.set_attribute("http.request.method", method)
                span.set_attribute("attacker.request_id", request_id)
                response = await call_next(request)
                span.set_attribute("http.response.status_code", response.status_code)
                return response
        finally:
            route = request.scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            status = str(response.status_code if response is not None else 500)
            HTTP_REQUESTS.labels(method=method, route=route_path, status=status).inc()
            HTTP_DURATION.labels(method=method, route=route_path).observe(perf_counter() - started)
            HTTP_IN_PROGRESS.dec()
            request_id_context.reset(token)
            if response is not None:
                response.headers[self.config.request_id_header] = request_id


def configure_observability(config: ObservabilitySettings) -> TracerProvider | None:
    global _tracer_provider
    if not config.tracing_enabled:
        return None
    if not config.otlp_endpoint:
        raise ValueError("tracing is enabled but OBSERVABILITY__OTLP_ENDPOINT is missing")
    if _tracer_provider is not None:
        return _tracer_provider
    headers = _parse_headers(config.otlp_headers)
    exporter = OTLPSpanExporter(endpoint=config.otlp_endpoint, headers=headers)
    provider = TracerProvider(
        resource=Resource.create(
            {
                SERVICE_NAME: config.service_name,
                DEPLOYMENT_ENVIRONMENT: config.deployment_environment,
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer_provider = provider
    return provider


def shutdown_observability() -> None:
    global _tracer_provider
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
        _tracer_provider = None


def metrics_response() -> Response:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        payload = generate_latest(registry)
    else:
        payload = generate_latest()
    return Response(payload, headers={"Content-Type": CONTENT_TYPE_LATEST})


def set_readiness(dependency: str, ready: bool) -> None:
    READINESS.labels(dependency=dependency).set(1 if ready else 0)


def record_equipment_counter(name: str, value: int) -> None:
    EQUIPMENT_EVENTS.labels(name=name).inc(value)


def record_equipment_invalid_packages(value: int) -> None:
    EQUIPMENT_INVALID_PACKAGES.set(value)


def record_equipment_duration(name: str, duration_ms: float) -> None:
    EQUIPMENT_DURATION.labels(name=name).observe(duration_ms / 1_000)


def record_job_event(event: str) -> None:
    JOB_EVENTS.labels(event=event).inc()


def refresh_job_metrics(snapshot: dict[str, Any]) -> None:
    status_counts = snapshot.get("status_counts", {})
    for status in JOB_STATUSES:
        JOB_STATE.labels(status=status).set(float(status_counts.get(status, 0)))
    JOB_OLDEST_READY_AGE.set(float(snapshot.get("oldest_ready_age_seconds", 0)))
    JOB_EXPIRED_LEASES.set(float(snapshot.get("expired_leases", 0)))
    WORKER_STALE.set(float(snapshot.get("stale_workers", 0)))
    METRICS_REFRESH_READY.set(1)


def record_metrics_refresh_failure() -> None:
    METRICS_REFRESH_READY.set(0)


def _parse_headers(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    headers: dict[str, str] = {}
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        if separator != "=" or not key.strip() or not value.strip():
            raise ValueError("OTLP headers must use comma-separated key=value pairs")
        headers[key.strip()] = value.strip()
    return headers
