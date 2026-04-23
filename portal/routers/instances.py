"""
routers/instances.py

REST API for tenant lifecycle.
Avoids any S3/boto3/Ceph — state is embedded in K8s objects.
"""
from __future__ import annotations
import asyncio
import base64
import logging
import os
import secrets
import string
import threading
from datetime import datetime

import httpx
import psycopg2
from psycopg2 import sql
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
import re

from k8s_utils.manifests import all_manifests, pdb_manifest, PLAN_RESOURCES, BASE_DOMAIN, URL_SCHEME, POSTGRES_HOST, POSTGRES_PORT, POSTGRES_PORT_PRIMARY, GIT_TOKEN
from k8s_utils.client import apply_manifest, delete_namespace, get_deployment_status, delete_pdb
from metrics import record_operation, record_error

# ── Odoo webhook push config ──────────────────────────────────────────────────
# Imported from main.py env vars; kept here for router-level access.
_ODOO_WEBHOOK_URL = os.getenv("ODOO_WEBHOOK_URL", "")
_ODOO_WEBHOOK_KEY = os.getenv("ODOO_WEBHOOK_KEY", "")


def _fire_webhook(tenant_id: str, status: str) -> None:
    """Push a status change to Odoo via webhook (best-effort, non-blocking).

    Called from background threads — never raises. If ODOO_WEBHOOK_URL is not
    set, the call is skipped and the 2-minute cron handles reconciliation.
    """
    if not _ODOO_WEBHOOK_URL or not _ODOO_WEBHOOK_KEY:
        return
    try:
        resp = httpx.post(
            _ODOO_WEBHOOK_URL,
            json={"tenant_id": tenant_id, "status": status},
            headers={"X-Webhook-Key": _ODOO_WEBHOOK_KEY, "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("webhook: pushed status=%s for %s → 200 OK", status, tenant_id)
        else:
            logger.warning(
                "webhook: push for %s returned %d: %s",
                tenant_id, resp.status_code, resp.text[:200],
            )
    except Exception as exc:
        logger.warning("webhook: failed to push status=%s for %s: %s", status, tenant_id, exc)


def _poll_until_ready_then_notify(tenant_id: str) -> None:
    """Background thread: poll K8s until the instance is ready, then fire webhook.

    Polls every 15 seconds for up to 20 minutes (80 attempts).
    This eliminates the 0-2 minute delay from the Odoo reconciliation cron.
    """
    import time
    max_attempts = 80  # 80 × 15s = 20 minutes
    interval = 15

    logger.info("webhook-poller: watching %s for readiness", tenant_id)
    for attempt in range(max_attempts):
        time.sleep(interval)
        try:
            info = get_deployment_status(f"odoo-{tenant_id}")
            if info["phase"] == "NotFound":
                logger.warning("webhook-poller: namespace for %s disappeared", tenant_id)
                _fire_webhook(tenant_id, "error")
                return
            if info.get("ready"):
                logger.info(
                    "webhook-poller: %s is ready after %d polls — firing webhook",
                    tenant_id, attempt + 1,
                )
                _fire_webhook(tenant_id, "ready")
                return
        except Exception as exc:
            logger.warning("webhook-poller: error checking %s: %s", tenant_id, exc)

    logger.warning("webhook-poller: %s did not become ready in %d min", tenant_id, max_attempts * interval // 60)
    _fire_webhook(tenant_id, "error")

logger = logging.getLogger(__name__)
router = APIRouter()

# Portal DB role — non-superuser (CREATEROLE + CREATEDB only, NOT superuser)
_PG_ADMIN_USER = os.getenv("POSTGRES_ADMIN_USER", "postgres")
_PG_ADMIN_PASSWORD = os.getenv("POSTGRES_ADMIN_PASSWORD", "")

# ── schemas ──────────────────────────────────────────────────────────────────

class CreateInstanceRequest(BaseModel):
    tenant_id: str          # slug: letters, numbers, hyphens
    plan: str = "starter"   # starter | pro | enterprise
    storage_gi: int = 10
    addons_repos: list = []
    odoo_version: str = "18.0"
    custom_image: str | None = None

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9\-]{0,46}[a-z0-9]$", v):
            raise ValueError("tenant_id must be lowercase alphanumeric/hyphens, 2-48 chars")
        return v


class InstanceResponse(BaseModel):
    tenant_id: str
    namespace: str
    url: str
    status: str
    user_count: int = 0
    app_admin_password: str = None


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/list", response_model=list[InstanceResponse])
def list_instances(user_count: bool = False):
    """List all active tenant instances by querying K8s namespaces.

    Returns one entry per odoo-* namespace (excluding odoo-admin, odoo-stg).
    Pass ?user_count=true to include live user counts (slower — one DB query per tenant).
    """
    from k8s_utils.client import list_tenant_namespaces

    namespaces = list_tenant_namespaces()
    result = []

    for namespace in namespaces:
        tenant_id = namespace.removeprefix("odoo-")
        info = get_deployment_status(namespace)

        if info["phase"] == "NotFound":
            status = "not_ready"
        elif info.get("ready"):
            status = "ready"
        else:
            status = "provisioning" if info["phase"] == "Pending" else "not_ready"

        count = 0
        if user_count and status == "ready":
            count = _get_user_count(tenant_id)

        result.append(InstanceResponse(
            tenant_id=tenant_id,
            namespace=namespace,
            url=f"{URL_SCHEME}://{tenant_id}.{BASE_DOMAIN}",
            status=status,
            user_count=count,
        ))

    return result


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
    except psycopg2.OperationalError as e:
        logger.warning("check_availability: PG connectivity failed for %s: %s", tenant_id, e)
        # Cannot confirm DB state — assume not taken so provisioning can proceed
    except Exception as e:
        logger.error("check_availability: unexpected error checking DB for %s: %s", tenant_id, e)

    available = not ns_taken and not db_taken
    return {
        "tenant_id": tenant_id,
        "available": available,
        "namespace_exists": ns_taken,
        "database_exists": db_taken,
    }


@router.post("", response_model=InstanceResponse, status_code=202)
def create_instance(req: CreateInstanceRequest, background_tasks: BackgroundTasks):
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
    app_admin_password = _gen_password(16)
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
        app_admin_password=app_admin_password,
        storage_gi=req.storage_gi,
        addons_repos=req.addons_repos,
        odoo_version=req.odoo_version,
        custom_image=req.custom_image,
        plan=req.plan,
        git_token=GIT_TOKEN,
    )

    for m in manifests:
        try:
            apply_manifest(m)
        except Exception as exc:
            logger.exception("Error applying manifest %s — rolling back", m.get("kind"))
            # Best-effort cleanup: don't let orphaned resources accumulate
            try:
                delete_namespace(namespace)
            except Exception:
                logger.warning("Rollback: could not delete namespace %s", namespace)
            try:
                _drop_pg_user(pg_user, db_name)
            except Exception:
                logger.warning("Rollback: could not drop pg user %s", pg_user)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    record_operation("provision")

    # Launch background poller: fires webhook when pod becomes ready.
    # Uses a daemon thread so it doesn't block the response or prevent shutdown.
    if _ODOO_WEBHOOK_URL:
        t = threading.Thread(
            target=_poll_until_ready_then_notify,
            args=(req.tenant_id,),
            daemon=True,
            name=f"webhook-poller-{req.tenant_id}",
        )
        t.start()

    return InstanceResponse(
        tenant_id=req.tenant_id,
        namespace=f"odoo-{req.tenant_id}",
        url=f"{URL_SCHEME}://{req.tenant_id}.{BASE_DOMAIN}",
        status="provisioning",
        app_admin_password=app_admin_password,
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
        url=f"{URL_SCHEME}://{tenant_id}.{BASE_DOMAIN}",
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

    record_operation("delete")
    threading.Thread(target=_fire_webhook, args=(tenant_id, "deleted"), daemon=True).start()
    # Drop Postgres user and database (best-effort; don't block the response)
    pg_user = f"odoo-{tenant_id}"
    db_name = f"odoo_{tenant_id}"
    try:
        _drop_pg_user(pg_user, db_name)
    except Exception as exc:
        logger.warning("Could not drop Postgres user %s: %s", pg_user, exc)

@router.post("/{tenant_id}/stop")
def stop_instance(tenant_id: str):
    """Suspend a tenant instance (scale to 0 and remove PDB to silence false alerts)."""
    namespace = f"odoo-{tenant_id}"
    from k8s_utils.client import scale_deployment
    try:
        scale_deployment(namespace, "odoo", 0)
        delete_pdb(namespace, "odoo-pdb")
    except Exception as exc:
        record_error("stop", "k8s_error")
        raise HTTPException(status_code=500, detail=str(exc))
    record_operation("stop")
    threading.Thread(target=_fire_webhook, args=(tenant_id, "stopped"), daemon=True).start()
    return {"status": "suspended"}

@router.post("/{tenant_id}/start")
def start_instance(tenant_id: str):
    """Resume a tenant instance (scale to 1 and restore PDB protection)."""
    namespace = f"odoo-{tenant_id}"
    from k8s_utils.client import scale_deployment
    try:
        scale_deployment(namespace, "odoo", 1)
        apply_manifest(pdb_manifest(tenant_id))
    except Exception as exc:
        record_error("start", "k8s_error")
        raise HTTPException(status_code=500, detail=str(exc))
    record_operation("start")
    threading.Thread(target=_fire_webhook, args=(tenant_id, "provisioning"), daemon=True).start()
    return {"status": "starting"}


class UpgradeRequest(BaseModel):
    plan: str = "starter"       # starter | pro | enterprise
    storage_gi: int | None = None  # Optional: expand PVC


@router.patch("/{tenant_id}/upgrade")
def upgrade_instance(tenant_id: str, req: UpgradeRequest):
    """Upgrade a running tenant to a different plan tier.

    Updates:
    1. ConfigMap (odoo.conf) — workers, cron_threads
    2. Deployment — CPU/RAM requests and limits
    3. Restarts the pod so changes take effect
    """
    namespace = f"odoo-{tenant_id}"
    from k8s_utils.client import (
        namespace_exists, read_namespaced_config_map,
        patch_namespaced_config_map, restart_deployment,
    )
    import re as _re

    if not namespace_exists(namespace):
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")

    if req.plan not in PLAN_RESOURCES:
        raise HTTPException(status_code=400, detail=f"Invalid plan: {req.plan}")

    res = PLAN_RESOURCES[req.plan]

    # ── 1. Patch ConfigMap: update workers and cron_threads in odoo.conf ──
    try:
        cm_data = read_namespaced_config_map(namespace, "odoo-conf")
        conf = cm_data.get("odoo.conf", "")
        # Replace workers = N and max_cron_threads = N
        conf = _re.sub(r'workers\s*=\s*\d+', f'workers = {res["workers"]}', conf)
        conf = _re.sub(r'max_cron_threads\s*=\s*\d+', f'max_cron_threads = {res["cron_threads"]}', conf)
        patch_namespaced_config_map(namespace, "odoo-conf", {"odoo.conf": conf})
    except Exception as exc:
        logger.exception("upgrade_instance: failed to patch ConfigMap for %s", tenant_id)
        raise HTTPException(status_code=500, detail=f"ConfigMap patch failed: {exc}") from exc

    # ── 2. Patch Deployment: update CPU/RAM resources ──
    try:
        from kubernetes import client as k8s_client
        from k8s_utils.client import _apps, _load_config
        _load_config()
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "name": "odoo",
                            "resources": {
                                "requests": {"cpu": res["cpu_req"], "memory": res["mem_req"]},
                                "limits":   {"cpu": res["cpu_lim"], "memory": res["mem_lim"]},
                            },
                        }]
                    }
                }
            }
        }
        _apps().patch_namespaced_deployment(name="odoo", namespace=namespace, body=patch_body)
    except Exception as exc:
        logger.exception("upgrade_instance: failed to patch Deployment for %s", tenant_id)
        raise HTTPException(status_code=500, detail=f"Deployment patch failed: {exc}") from exc

    # ── 3. Restart to apply new odoo.conf ──
    try:
        restart_deployment(namespace, "odoo")
    except Exception as exc:
        logger.exception("upgrade_instance: failed to restart Deployment for %s", tenant_id)
        raise HTTPException(status_code=500, detail=f"Restart failed: {exc}") from exc

    logger.info("upgrade_instance: %s upgraded to plan '%s' (workers=%d, cpu=%s, mem=%s)",
                tenant_id, req.plan, res["workers"], res["cpu_lim"], res["mem_lim"])
    record_operation("upgrade")
    return {"status": "upgrading", "plan": req.plan}


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
    """Connect to PostgreSQL via HAProxy primary (port 5000)."""
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=dbname,
        user=_PG_ADMIN_USER,
        password=_PG_ADMIN_PASSWORD,
    )


def _pg_admin_conn(dbname: str = "postgres"):
    """Connect to PostgreSQL primary (port 5000) — for DDL: CREATE/DROP ROLE/DATABASE.

    Kept as separate function for clarity. Uses same port as _pg_conn()
    since PgBouncer was removed from the architecture.
    """
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=dbname,
        user=_PG_ADMIN_USER,
        password=_PG_ADMIN_PASSWORD,
    )

def _get_user_count(tenant_id: str) -> int:
    """Connect directly to the tenant database to count paying users.

    Excludes system/technical users that should not be billed:
    - __system__: Odoo internal system user (UID 1)
    - share=true: portal/external users
    - active=false: deactivated users
    """
    db_name = f"odoo_{tenant_id}"
    try:
        conn = _pg_conn(dbname=db_name)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM res_users
                WHERE share = false
                  AND active = true
                  AND login NOT IN ('__system__')
            """)
            count = cur.fetchone()[0]
        conn.close()
        return count
    except psycopg2.OperationalError as e:
        logger.error("_get_user_count: DB connectivity failure for tenant %s: %s", tenant_id, e)
        return -1  # -1 signals connectivity error (distinct from 0 users)
    except psycopg2.ProgrammingError as e:
        # Table may not exist yet during provisioning — not a connectivity issue
        logger.warning("_get_user_count: schema not ready for %s: %s", tenant_id, e)
        return 0
    except Exception as e:
        logger.warning("_get_user_count: unexpected error for %s: %s", tenant_id, e)
        return 0


def _create_pg_user(pg_user: str, password: str, db_name: str) -> None:
    """Create a dedicated Postgres role + database for a tenant (idempotent)."""
    conn = _pg_admin_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (pg_user,))
            if cur.fetchone():
                # Role exists — always sync password so K8s secret stays consistent
                cur.execute(
                    sql.SQL('ALTER ROLE {} PASSWORD %s').format(sql.Identifier(pg_user)),
                    (password,),
                )
                logger.info("Updated password for existing Postgres role %s", pg_user)
            else:
                cur.execute(
                    sql.SQL('CREATE ROLE {} LOGIN PASSWORD %s').format(sql.Identifier(pg_user)),
                    (password,),
                )
                logger.info("Created Postgres role %s", pg_user)

            # PG 16+ requires the admin user to hold membership in the target
            # role before it can CREATE DATABASE ... OWNER <role>.
            cur.execute(
                sql.SQL('GRANT {} TO {}').format(
                    sql.Identifier(pg_user), sql.Identifier(_PG_ADMIN_USER)
                )
            )

            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if not cur.fetchone():
                cur.execute(
                    sql.SQL('CREATE DATABASE {} OWNER {}').format(
                        sql.Identifier(db_name), sql.Identifier(pg_user)
                    )
                )
                logger.info("Created Postgres database %s", db_name)
    finally:
        conn.close()


def _drop_pg_user(pg_user: str, db_name: str) -> None:
    """Drop tenant Postgres database and role.

    Terminates active connections first to avoid
    'database is being accessed by other users' errors.
    """
    conn = _pg_admin_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # Kick non-superuser connections off the database first.
            # pg_terminate_backend requires SUPERUSER to terminate other superuser
            # processes (e.g. pg_exporter), so we skip those to avoid permission errors.
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid() "
                "AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = usename AND rolsuper)",
                (db_name,),
            )
            cur.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(db_name)))
            cur.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(pg_user)))
            logger.info("Dropped Postgres role/db for %s", pg_user)
    finally:
        conn.close()


@router.get("/{tenant_id}/backup")
async def download_backup(tenant_id: str):
    """Stream a complete Odoo backup (DB + filestore ZIP) for a tenant.

    Uses kubectl exec to run dump_db directly inside the tenant pod,
    bypassing the list_db=False restriction that blocks the HTTP and
    XML-RPC backup endpoints in Odoo 18. Outputs base64-encoded ZIP
    via stdout, decoded and streamed back to the caller.
    """
    if not re.match(r"^[a-z0-9][a-z0-9\-]{0,46}[a-z0-9]$", tenant_id):
        raise HTTPException(status_code=422, detail="Invalid tenant_id format")
    namespace = f"odoo-{tenant_id}"

    # ── Retrieve secret (for validation only — exec uses pod's own config) ─
    from k8s_utils.client import _core
    try:
        secret = _core().read_namespaced_secret("odoo-secret", namespace)
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status == 404:
            raise HTTPException(status_code=404, detail=f"Instance '{tenant_id}' not found")
        raise HTTPException(status_code=500, detail=f"Cannot read K8s secret: {exc}")

    if not secret.data.get("ADMIN_PASSWD"):
        raise HTTPException(status_code=500, detail="admin_passwd not found in secret")

    # ── Find a running pod ────────────────────────────────────────────────
    try:
        pods = _core().list_namespaced_pod(namespace, label_selector="app=odoo")
        running = [p for p in pods.items if p.status.phase == "Running"]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cannot list pods: {exc}")
    if not running:
        raise HTTPException(status_code=503, detail=f"Instance '{tenant_id}' has no running pods (may be suspended)")
    pod_name = running[0].metadata.name

    db_name = f"odoo_{tenant_id}"
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{tenant_id}-backup-{date_str}.zip"

    # ── Build the Python one-liner to exec inside the pod ─────────────────
    # Patches list_db=True in-process (no config file change) so that
    # dump_db's @if_db_mgt_enabled decorator allows the call.
    python_cmd = (
        "import sys,base64,io;"
        "sys.path.insert(0,'/opt/odoo');"
        "import odoo;"
        "from odoo.tools import config;"
        "config.parse_config(['--config=/etc/odoo/odoo.conf']);"
        "config['list_db']=True;"
        "from odoo.service.db import dump_db;"
        "buf=io.BytesIO();"
        f"dump_db('{db_name}',buf,'zip');"
        "sys.stdout.buffer.write(base64.b64encode(buf.getvalue()));"
        "sys.stdout.buffer.flush()"
    )

    # ── Execute inside pod via kubernetes stream (synchronous — run in thread)
    def _exec_backup() -> tuple[bytes, str]:
        from kubernetes.stream import stream as k8s_stream
        resp = k8s_stream(
            _core().connect_get_namespaced_pod_exec,
            pod_name, namespace,
            command=["python3", "-c", python_cmd],
            container="odoo",
            stderr=True, stdin=False, stdout=True, tty=False,
            _preload_content=False,
        )
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[str] = []
        while resp.is_open():
            resp.update(timeout=600)
            if resp.peek_stdout():
                chunk = resp.read_stdout()
                stdout_chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
            if resp.peek_stderr():
                stderr_chunks.append(resp.read_stderr())
        resp.close()
        return b"".join(stdout_chunks), "".join(stderr_chunks)

    try:
        b64_bytes, stderr_out = await asyncio.to_thread(_exec_backup)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Backup exec failed: {exc}")

    if not b64_bytes:
        raise HTTPException(
            status_code=500,
            detail=f"Backup produced no output. stderr: {stderr_out[:400]}",
        )

    try:
        zip_data = base64.b64decode(b64_bytes)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Backup output is not valid base64: {exc}")

    if zip_data[:4] != b"PK\x03\x04":
        raise HTTPException(
            status_code=500,
            detail=f"Backup produced invalid ZIP (magic={zip_data[:4]!r}). stderr: {stderr_out[:200]}",
        )

    async def stream_zip():
        chunk_size = 1024 * 1024  # 1 MB chunks
        for i in range(0, len(zip_data), chunk_size):
            yield zip_data[i : i + chunk_size]

    return StreamingResponse(
        stream_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── helpers ──────────────────────────────────────────────────────────────────

def _gen_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
