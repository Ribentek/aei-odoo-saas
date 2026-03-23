"""
routers/instances.py

REST API for tenant lifecycle.
Avoids any S3/boto3/Ceph — state is embedded in K8s objects.
"""
from __future__ import annotations
import logging
import os
import secrets
import string

import psycopg2
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
import re

from k8s_utils.manifests import all_manifests, BASE_DOMAIN, POSTGRES_HOST, POSTGRES_PORT
from k8s_utils.client import apply_manifest, delete_namespace, get_deployment_status

logger = logging.getLogger(__name__)
router = APIRouter()

# Postgres superuser used only by the portal to create/drop tenant users
_PG_ADMIN_USER = os.getenv("POSTGRES_ADMIN_USER", "postgres")
_PG_ADMIN_PASSWORD = os.getenv("POSTGRES_ADMIN_PASSWORD", "")

# ── schemas ──────────────────────────────────────────────────────────────────

class CreateInstanceRequest(BaseModel):
    tenant_id: str          # slug: letters, numbers, hyphens
    plan: str = "starter"   # starter | pro | enterprise
    storage_gi: int = 10
    addons_repos: list = []

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9\-]{0,30}[a-z0-9]$", v):
            raise ValueError("tenant_id must be lowercase alphanumeric/hyphens, 2-32 chars")
        return v


class InstanceResponse(BaseModel):
    tenant_id: str
    namespace: str
    url: str
    status: str
    user_count: int = 0


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/check/{tenant_id}")
def check_availability(tenant_id: str):
    """Check whether a tenant_id is available (namespace + DB don't exist)."""
    namespace = f"odoo-{tenant_id}"
    from k8s_utils.client import namespace_exists
    ns_taken = namespace_exists(namespace)

    db_name = f"odoo_{tenant_id}"
    db_taken = False
    try:
        conn = _pg_conn()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            db_taken = cur.fetchone() is not None
        conn.close()
    except Exception:
        pass  # If PG is unreachable, just check namespace

    available = not ns_taken and not db_taken
    return {
        "tenant_id": tenant_id,
        "available": available,
        "namespace_exists": ns_taken,
        "database_exists": db_taken,
    }


@router.post("", response_model=InstanceResponse, status_code=202)
def create_instance(req: CreateInstanceRequest):
    """
    Provision a new Odoo tenant.
    1. Creates a dedicated Postgres role + database.
    2. Applies K8s manifests (namespace, secret, configmap, deployment, …).
    Returns immediately; poll GET /instances/{id} for readiness.
    """
    # Guard: reject duplicate tenant IDs immediately
    namespace = f"odoo-{req.tenant_id}"
    from k8s_utils.client import namespace_exists
    if namespace_exists(namespace):
        raise HTTPException(
            status_code=409,
            detail=f"Tenant '{req.tenant_id}' already exists. Choose a different name.",
        )

    db_password = _gen_password()
    admin_password = _gen_password()
    pg_user = f"odoo-{req.tenant_id}"
    db_name = f"odoo_{req.tenant_id}"

    # Step 1 — Postgres user + database
    try:
        _create_pg_user(pg_user, db_password, db_name)
    except Exception as exc:
        logger.exception("Failed to create Postgres user %s", pg_user)
        raise HTTPException(status_code=500, detail=f"Postgres setup failed: {exc}") from exc

    # Step 2 — K8s manifests
    manifests = all_manifests(
        tenant_id=req.tenant_id,
        db_password=db_password,
        admin_password=admin_password,
        storage_gi=req.storage_gi,
        addons_repos=req.addons_repos,
    )

    for m in manifests:
        try:
            apply_manifest(m)
        except Exception as exc:
            logger.exception("Error applying manifest %s", m.get("kind"))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return InstanceResponse(
        tenant_id=req.tenant_id,
        namespace=f"odoo-{req.tenant_id}",
        url=f"https://{req.tenant_id}.{BASE_DOMAIN}",
        status="provisioning",
    )


@router.get("/{tenant_id}", response_model=InstanceResponse)
def get_instance(tenant_id: str):
    """Poll the readiness of a tenant instance."""
    namespace = f"odoo-{tenant_id}"
    info = get_deployment_status(namespace)

    if info["phase"] == "NotFound":
        raise HTTPException(status_code=404, detail="Instance not found")

    status = "ready" if info["ready"] else "provisioning"
    user_count = 0
    if status == "ready":
        user_count = _get_user_count(tenant_id)
        
    return InstanceResponse(
        tenant_id=tenant_id,
        namespace=namespace,
        url=f"https://{tenant_id}.{BASE_DOMAIN}",
        status=status,
        user_count=user_count,
    )


@router.delete("/{tenant_id}", status_code=204)
def delete_instance(tenant_id: str):
    """Delete all K8s resources for a tenant by deleting its namespace."""
    namespace = f"odoo-{tenant_id}"
    try:
        delete_namespace(namespace)
    except Exception as exc:
        logger.exception("Failed to delete namespace %s", namespace)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Drop Postgres user and database (best-effort; don't block the response)
    pg_user = f"odoo-{tenant_id}"
    db_name = f"odoo_{tenant_id}"
    try:
        _drop_pg_user(pg_user, db_name)
    except Exception as exc:
        logger.warning("Could not drop Postgres user %s: %s", pg_user, exc)

@router.post("/{tenant_id}/stop")
def stop_instance(tenant_id: str):
    """Suspend a tenant instance (scale to 0)."""
    namespace = f"odoo-{tenant_id}"
    from k8s_utils.client import scale_deployment
    try:
        scale_deployment(namespace, "odoo", 0)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "suspended"}

@router.post("/{tenant_id}/start")
def start_instance(tenant_id: str):
    """Resume a tenant instance (scale to 1)."""
    namespace = f"odoo-{tenant_id}"
    from k8s_utils.client import scale_deployment
    try:
        scale_deployment(namespace, "odoo", 1)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "starting"}


class ConfigUpdateRequest(BaseModel):
    odoo_conf: str = None
    addons_repos: list = None

@router.get("/{tenant_id}/config")
def get_instance_config(tenant_id: str):
    namespace = f"odoo-{tenant_id}"
    from k8s_utils.client import read_namespaced_config_map
    try:
        data = read_namespaced_config_map(namespace, "odoo-conf")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    
    import json
    addons = []
    if "addons.json" in data:
        try:
            addons = json.loads(data["addons.json"])
        except:
            pass
    return {"odoo_conf": data.get("odoo.conf", ""), "addons_repos": addons}

@router.put("/{tenant_id}/config")
def update_instance_config(tenant_id: str, req: ConfigUpdateRequest):
    if req.odoo_conf is None:
        raise HTTPException(status_code=400, detail="odoo_conf is required for PUT")
    return patch_instance_config(tenant_id, req)

@router.patch("/{tenant_id}/config")
def patch_instance_config(tenant_id: str, req: ConfigUpdateRequest):
    namespace = f"odoo-{tenant_id}"
    from k8s_utils.client import patch_namespaced_config_map, restart_deployment
    import json
    update_data = {}
    if req.odoo_conf is not None:
        update_data["odoo.conf"] = req.odoo_conf
    if req.addons_repos is not None:
        update_data["addons.json"] = json.dumps(req.addons_repos)
        
    if not update_data:
        return {"status": "no change"}
        
    try:
        patch_namespaced_config_map(namespace, "odoo-conf", update_data)
        restart_deployment(namespace, "odoo")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "restarting"}

@router.get("/{tenant_id}/logs")
def get_instance_logs(tenant_id: str, lines: int = 200):
    namespace = f"odoo-{tenant_id}"
    from k8s_utils.client import read_namespaced_pod_log
    try:
        logs = read_namespaced_pod_log(namespace, "app=odoo", lines)
        return {"logs": logs}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Postgres helpers ─────────────────────────────────────────────────────────

def _pg_conn(dbname: str = "postgres"):
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=dbname,
        user=_PG_ADMIN_USER,
        password=_PG_ADMIN_PASSWORD,
    )

def _get_user_count(tenant_id: str) -> int:
    """Connect directly to the tenant database to count paying users."""
    db_name = f"odoo_{tenant_id}"
    try:
        conn = _pg_conn(dbname=db_name)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM res_users WHERE share=false AND active=true")
            count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.warning("Could not fetch user count for %s: %s", tenant_id, e)
        return 0


def _create_pg_user(pg_user: str, password: str, db_name: str) -> None:
    """Create a dedicated Postgres role + database for a tenant (idempotent)."""
    conn = _pg_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (pg_user,))
            if cur.fetchone():
                # Role exists — always sync password so K8s secret stays consistent
                cur.execute(f'ALTER ROLE "{pg_user}" PASSWORD %s', (password,))
                logger.info("Updated password for existing Postgres role %s", pg_user)
            else:
                cur.execute(f'CREATE ROLE "{pg_user}" LOGIN PASSWORD %s', (password,))
                logger.info("Created Postgres role %s", pg_user)

            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{db_name}" OWNER "{pg_user}"')
                logger.info("Created Postgres database %s", db_name)
    finally:
        conn.close()


def _drop_pg_user(pg_user: str, db_name: str) -> None:
    """Drop tenant Postgres database and role.

    Terminates active connections first to avoid
    'database is being accessed by other users' errors.
    """
    conn = _pg_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # Kick everyone off the database first
            cur.execute(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            cur.execute(f'DROP ROLE IF EXISTS "{pg_user}"')
            logger.info("Dropped Postgres role/db for %s", pg_user)
    finally:
        conn.close()


# ── helpers ──────────────────────────────────────────────────────────────────

def _gen_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
