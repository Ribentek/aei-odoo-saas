"""
k8s_utils/manifests.py

Generates Kubernetes manifest dicts for a tenant Odoo deployment.
Designed for k3s with:
  - Shared postgres on postgres.aeisoftware.svc.cluster.local
  - Traefik IngressRoute with wildcard via Cloudflare tunnel
  - local-path storage class
  - No Ceph, no S3, no Patroni
"""
from __future__ import annotations
import os
from typing import Any

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "aeisoftware.com")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres.aeisoftware.svc.cluster.local")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_USER = os.getenv("POSTGRES_USER", "odoo")
ODOO_IMAGE = os.getenv("ODOO_IMAGE", "odoo:18")

ODOO_HEADERS_MIDDLEWARE = "kube-system-odoo-headers@kubernetescrd"


def namespace_manifest(tenant_id: str) -> dict[str, Any]:
    """Namespace for one tenant: odoo-<tenant_id>"""
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": _ns(tenant_id),
            "labels": {
                "managed-by": "saas-portal",
                "tenant": tenant_id,
            },
        },
    }


def pvc_manifest(tenant_id: str, storage_gi: int = 10) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "odoo-data",
            "namespace": _ns(tenant_id),
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "local-path",
            "resources": {"requests": {"storage": f"{storage_gi}Gi"}},
        },
    }


def secret_manifest(tenant_id: str, db_password: str, admin_password: str) -> dict[str, Any]:
    """Per-tenant secret with DB password and Odoo admin password."""
    import base64
    def b64(s: str) -> str:
        return base64.b64encode(s.encode()).decode()

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "odoo-secret",
            "namespace": _ns(tenant_id),
        },
        "type": "Opaque",
        "data": {
            "DB_PASSWORD": b64(db_password),
            "ADMIN_PASSWD": b64(admin_password),
        },
    }


def configmap_manifest(tenant_id: str, db_password: str, admin_password: str, addons_repos: list = None) -> dict[str, Any]:
    """Odoo config file per tenant — passwords are embedded at provision time."""
    db_name = _dbname(tenant_id)
    addons_repos = addons_repos or []
    import json
    addons_json_str = json.dumps(addons_repos)

    conf = f"""[options]
db_host = {POSTGRES_HOST}
db_port = {POSTGRES_PORT}
db_user = odoo-{tenant_id}
db_password = {db_password}
admin_passwd = {admin_password}
db_name = {db_name}
dbfilter = ^{db_name}$
list_db = False
addons_path = /usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons
data_dir = /var/lib/odoo
workers = 2
max_cron_threads = 1
gevent_port = 8072
proxy_mode = True
without_demo = all
"""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "odoo-conf",
            "namespace": _ns(tenant_id),
        },
        "data": {
            "odoo.conf": conf,
            "addons.json": addons_json_str
        },
    }



def deployment_manifest(tenant_id: str) -> dict[str, Any]:
    pg_user = f"odoo-{tenant_id}"
    # Shared volume mounts and env used by both init and main containers
    _vol_mounts = [
        {"name": "odoo-conf", "mountPath": "/etc/odoo"},
        {"name": "odoo-data", "mountPath": "/var/lib/odoo"},
        {"name": "odoo-extra-addons", "mountPath": "/mnt/extra-addons"},
    ]
    _env = [
        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "odoo-secret", "key": "DB_PASSWORD"}}},
        {"name": "HOST",        "value": POSTGRES_HOST},
        {"name": "PORT",        "value": str(POSTGRES_PORT)},
        {"name": "USER",        "value": pg_user},
        {"name": "PASSWORD",    "valueFrom": {"secretKeyRef": {"name": "odoo-secret", "key": "DB_PASSWORD"}}},
    ]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "odoo",
            "namespace": _ns(tenant_id),
            "labels": {"app": "odoo", "tenant": tenant_id},
        },
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": "odoo"}},
            "template": {
                "metadata": {"labels": {"app": "odoo", "tenant": tenant_id}},
                "spec": {
                    # Init container: bootstrap the DB schema (workers>0 mode can't do this)
                    "initContainers": [
                        {
                            "name": "clone-addons",
                            "image": "python:3.10-alpine",
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                "apk add --no-cache git && python3 -c '\n"
                                "import json, os, subprocess\n"
                                "try:\n"
                                "    with open(\"/etc/odoo/addons.json\") as f:\n"
                                "        addons = json.load(f)\n"
                                "except Exception:\n"
                                "    addons = []\n"
                                "for repo in addons:\n"
                                "    url = repo.get(\"url\")\n"
                                "    branch = repo.get(\"branch\", \"\")\n"
                                "    if not url: continue\n"
                                "    repo_name = url.rstrip(\"/\").rsplit(\"/\", 1)[-1]\n"
                                "    if repo_name.endswith(\".git\"): repo_name = repo_name[:-4]\n"
                                "    dest = f\"/mnt/extra-addons/{repo_name}\"\n"
                                "    cmd = [\"git\", \"clone\", \"--depth=1\"]\n"
                                "    if branch:\n"
                                "        cmd.extend([\"-b\", branch])\n"
                                "    cmd.extend([url, dest])\n"
                                "    print(f\"Cloning {url} branch {branch} into {dest}...\")\n"
                                "    if not os.path.exists(dest):\n"
                                "        subprocess.run(cmd, check=True)\n"
                                "'"
                            ],
                            "volumeMounts": _vol_mounts,
                        },
                        {
                            "name": "odoo-init",
                            "image": ODOO_IMAGE,
                            "args": [
                                "--config=/etc/odoo/odoo.conf",
                                "--init=base",
                                "--stop-after-init",
                            ],
                            "env": _env,
                            "volumeMounts": _vol_mounts,
                        }
                    ],
                    "containers": [
                        {
                            "name": "odoo",
                            "image": ODOO_IMAGE,
                            "args": ["--config=/etc/odoo/odoo.conf"],
                            "ports": [
                                {"containerPort": 8069},
                                {"containerPort": 8072},
                            ],
                            "env": _env,
                            "volumeMounts": _vol_mounts,
                            "readinessProbe": {
                                "httpGet": {"path": "/web/health", "port": 8069},
                                "initialDelaySeconds": 30,
                                "periodSeconds": 15,
                                "failureThreshold": 40,
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "512Mi"},
                                "limits":   {"cpu": "1",    "memory": "2Gi"},
                            },
                        }
                    ],
                    "volumes": [
                        {"name": "odoo-conf", "configMap": {"name": "odoo-conf"}},
                        {"name": "odoo-data", "persistentVolumeClaim": {"claimName": "odoo-data"}},
                        {"name": "odoo-extra-addons", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def service_manifest(tenant_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "odoo", "namespace": _ns(tenant_id)},
        "spec": {
            "selector": {"app": "odoo"},
            "ports": [
                {"name": "http", "port": 8069, "targetPort": 8069},
                {"name": "longpoll", "port": 8072, "targetPort": 8072},
            ],
        },
    }


def ingress_manifest(tenant_id: str) -> dict[str, Any]:
    """Standard K8s Ingress for Traefik."""
    subdomain = tenant_id  # e.g. demo → demo.aeisoftware.com
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": "odoo-ingress",
            "namespace": _ns(tenant_id),
            "annotations": {
                "traefik.ingress.kubernetes.io/router.entrypoints": "web",
            },
        },
        "spec": {
            "ingressClassName": "traefik",
            "rules": [
                {
                    "host": f"{subdomain}.{BASE_DOMAIN}",
                    "http": {
                        "paths": [
                            {
                                "path": "/websocket",
                                "pathType": "Prefix",
                                "backend": {"service": {"name": "odoo", "port": {"number": 8072}}},
                            },
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {"service": {"name": "odoo", "port": {"number": 8069}}},
                            },
                        ]
                    },
                }
            ],
        },
    }


def all_manifests(tenant_id: str, db_password: str, admin_password: str, storage_gi: int = 10, addons_repos: list = None) -> list[dict]:
    """Return all manifests in apply-order."""
    return [
        namespace_manifest(tenant_id),
        pvc_manifest(tenant_id, storage_gi),
        secret_manifest(tenant_id, db_password, admin_password),
        configmap_manifest(tenant_id, db_password, admin_password, addons_repos),
        deployment_manifest(tenant_id),
        service_manifest(tenant_id),
        ingress_manifest(tenant_id),
    ]



# ── helpers ──────────────────────────────────────────────────────────────────
def _ns(tenant_id: str) -> str:
    return f"odoo-{tenant_id}"


def _dbname(tenant_id: str) -> str:
    return f"odoo_{tenant_id}"
