"""
SaaS Portal — main entrypoint.

Provides:
  POST /api/v1/instances       — provision a new Odoo tenant
  GET  /api/v1/instances/{id}  — status of a tenant
  DELETE /api/v1/instances/{id} — tear down a tenant
  GET  /healthz                — liveness probe
"""
import logging
import os

from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.responses import Response
from contextlib import asynccontextmanager
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from routers import instances, gc
from metrics import refresh_tenant_gauges

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

API_KEY = os.getenv("API_KEY", "changeme")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


def verify_key(key: str = Security(api_key_header)):
    if key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    return key


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("SaaS portal starting …")
    refresh_tenant_gauges()
    yield
    logger.info("SaaS portal shutting down …")


app = FastAPI(title="Odoo SaaS Portal", lifespan=lifespan)

# ── Prometheus metrics ─────────────────────────────────────────────────────────
Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/healthz", "/metrics"],
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


@app.get("/metrics/tenants", include_in_schema=False)
def metrics_tenants():
    """Re-fresh tenant state gauges and return all metrics."""
    refresh_tenant_gauges()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

app.include_router(
    instances.router,
    prefix="/api/v1/instances",
    tags=["instances"],
    dependencies=[Depends(verify_key)],
)

app.include_router(
    gc.router,
    prefix="/api/v1/gc",
    tags=["gc"],
    dependencies=[Depends(verify_key)],
)


@app.get("/healthz")
def healthz():
    """Liveness probe — confirms the process is alive. No dependency checks."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness probe — verifies PG and K8s API are reachable.

    Returns 503 if any dependency is down so K8s stops routing traffic
    to this pod without restarting it.
    """
    import psycopg2
    from kubernetes import client as k8s_client, config as k8s_config

    checks: dict[str, str] = {}
    failed = False

    # ── PostgreSQL check ──────────────────────────────────────────────────────
    try:
        conn = psycopg2.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            dbname="postgres",
            user=os.getenv("POSTGRES_ADMIN_USER", "postgres"),
            password=os.getenv("POSTGRES_ADMIN_PASSWORD", ""),
            connect_timeout=2,
        )
        conn.close()
        checks["postgres"] = "ok"
    except Exception as exc:
        logger.warning("readyz: postgres check failed: %s", exc)
        checks["postgres"] = f"error: {exc}"
        failed = True

    # ── Kubernetes API check ──────────────────────────────────────────────────
    try:
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        k8s_client.CoreV1Api().list_namespace(limit=1, timeout_seconds=2)
        checks["kubernetes"] = "ok"
    except Exception as exc:
        logger.warning("readyz: kubernetes check failed: %s", exc)
        checks["kubernetes"] = f"error: {exc}"
        failed = True

    if failed:
        return Response(
            content=str({"status": "degraded", "checks": checks}),
            status_code=503,
            media_type="application/json",
        )
    return {"status": "ok", "checks": checks}
