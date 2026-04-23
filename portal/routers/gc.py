"""
routers/gc.py

Garbage-collection endpoints for orphaned Kubernetes and Postgres resources.

GET  /api/v1/gc/pvs          — list Released PVs for deleted tenant namespaces
DELETE /api/v1/gc/pvs        — delete those PVs (pass ?dry_run=true to preview)

GET  /api/v1/gc/dbs          — list orphaned PG databases (no matching K8s namespace)
DELETE /api/v1/gc/dbs        — drop them (pass ?dry_run=true to preview)
"""
from __future__ import annotations
import logging
import os

import psycopg2
from psycopg2 import sql
from fastapi import APIRouter, Query

from k8s_utils.client import list_released_pvs, delete_pv, namespace_exists

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Postgres connection ───────────────────────────────────────────────────────

_PG_HOST     = os.getenv("POSTGRES_HOST", "postgres.aeisoftware.svc.cluster.local")
_PG_PORT     = int(os.getenv("POSTGRES_PORT_PRIMARY", os.getenv("POSTGRES_PORT", "5000")))
_PG_ADMIN_USER = os.getenv("POSTGRES_ADMIN_USER", "postgres")
_PG_ADMIN_PASSWORD = os.getenv("POSTGRES_ADMIN_PASSWORD", "")

# DBs that must never be dropped regardless of K8s state
_PROTECTED_DBS = {"postgres", "admin", "staging", "template0", "template1"}


def _pg_conn():
    return psycopg2.connect(
        host=_PG_HOST, port=_PG_PORT,
        dbname="postgres",
        user=_PG_ADMIN_USER, password=_PG_ADMIN_PASSWORD,
    )


def _find_orphaned_dbs() -> list[dict]:
    """Return PG databases whose K8s namespace no longer exists.

    A DB named ``odoo_<tenant_id>`` is orphaned when the namespace
    ``odoo-<tenant_id>`` does not exist in Kubernetes.
    Protected DBs (postgres, admin, staging, templates) are always excluded.
    """
    conn = _pg_conn()
    conn.autocommit = True
    orphans = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT datname, pg_size_pretty(pg_database_size(datname)) "
                "FROM pg_database "
                "WHERE datname LIKE 'odoo_%' "
                "ORDER BY datname"
            )
            rows = cur.fetchall()

            for datname, size in rows:
                if datname in _PROTECTED_DBS:
                    continue
                # odoo_<tenant_id> → namespace odoo-<tenant_id>
                tenant_id = datname[len("odoo_"):]
                namespace = f"odoo-{tenant_id}"
                if not namespace_exists(namespace):
                    # Also check whether a matching PG role exists
                    role = f"odoo-{tenant_id}"
                    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                    has_role = cur.fetchone() is not None
                    orphans.append({
                        "tenant_id": tenant_id,
                        "db_name": datname,
                        "role_name": role if has_role else None,
                        "size": size,
                        "namespace": namespace,
                    })
    finally:
        conn.close()
    return orphans


@router.get("/pvs")
def get_released_pvs():
    """List PersistentVolumes in Released phase for deleted tenant namespaces."""
    pvs = list_released_pvs()
    return {"count": len(pvs), "pvs": pvs}


@router.delete("/pvs")
def delete_released_pvs(dry_run: bool = Query(False, description="Preview without deleting")):
    """Delete orphaned PersistentVolumes left behind after tenant deletion.

    Pass ?dry_run=true to list what would be deleted without actually deleting.
    Returns the list of PVs acted on (or that would be acted on).
    """
    pvs = list_released_pvs()
    deleted = []
    errors = []

    for pv in pvs:
        if dry_run:
            deleted.append(pv["name"])
            continue
        try:
            delete_pv(pv["name"])
            logger.info("gc: deleted Released PV %s (was bound to %s/%s)",
                        pv["name"], pv["claim_namespace"], pv["claim_name"])
            deleted.append(pv["name"])
        except Exception as exc:
            logger.exception("gc: failed to delete PV %s: %s", pv["name"], exc)
            errors.append({"pv": pv["name"], "error": str(exc)})

    return {
        "dry_run": dry_run,
        "deleted": deleted,
        "errors": errors,
    }


# ── DB garbage collection ─────────────────────────────────────────────────────

@router.get("/dbs")
def get_orphaned_dbs():
    """List Postgres databases whose K8s namespace no longer exists.

    A database ``odoo_<tenant>`` is orphaned when the namespace
    ``odoo-<tenant>`` is gone. Protected DBs (admin, staging, postgres,
    templates) are always excluded.
    """
    orphans = _find_orphaned_dbs()
    total_size_note = f"{len(orphans)} orphaned DB(s) found"
    return {"count": len(orphans), "note": total_size_note, "orphans": orphans}


@router.delete("/dbs")
def delete_orphaned_dbs(dry_run: bool = Query(False, description="Preview without deleting")):
    """Drop orphaned Postgres databases and their roles.

    Pass ``?dry_run=true`` to list what would be dropped without acting.
    Only databases whose K8s namespace is fully gone are eligible.
    """
    orphans = _find_orphaned_dbs()
    dropped = []
    errors = []

    conn = _pg_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for o in orphans:
                db_name = o["db_name"]
                role_name = o["role_name"]
                if dry_run:
                    dropped.append({"db": db_name, "role": role_name, "size": o["size"]})
                    continue
                try:
                    # Kick non-superuser connections off the database first.
                    # pg_terminate_backend requires SUPERUSER to terminate other superuser
                    # processes (e.g. pg_exporter), so we skip those to avoid permission errors.
                    cur.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid() "
                        "AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = usename AND rolsuper)",
                        (db_name,),
                    )
                    cur.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name))
                    )
                    if role_name:
                        cur.execute(
                            sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name))
                        )
                    logger.info("gc/dbs: dropped db=%s role=%s (%s)", db_name, role_name, o["size"])
                    dropped.append({"db": db_name, "role": role_name, "size": o["size"]})
                except Exception as exc:
                    logger.exception("gc/dbs: failed to drop %s: %s", db_name, exc)
                    errors.append({"db": db_name, "error": str(exc)})
    finally:
        conn.close()

    return {"dry_run": dry_run, "dropped": dropped, "errors": errors}
