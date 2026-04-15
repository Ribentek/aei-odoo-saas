"""
k8s_utils/manifests.py

Generates Kubernetes manifest dicts for a tenant Odoo deployment.
Fase 3 — K3s HA con Ceph RBD storage y PostgreSQL HA externo:
  - PostgreSQL HA en 192.168.0.127/.186/.226 via HAProxy
  - :5000 HAProxy primary directo (all traffic)
  - LISTEN/NOTIFY nativo (longpolling sin bus_alt_connection)
  - ceph-rbd StorageClass (pool k3s-rbd)
  - Cilium NetworkPolicy con egress a 192.168.0.0/24
"""
from __future__ import annotations
import os
from typing import Any

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "aeisoftware.com")
URL_SCHEME = os.getenv("URL_SCHEME", "http")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres.aeisoftware.svc.cluster.local")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5000"))          # HAProxy primary
POSTGRES_PORT_PRIMARY = int(os.getenv("POSTGRES_PORT_PRIMARY", "5000"))  # Same (legacy compat)
POSTGRES_USER = os.getenv("POSTGRES_USER", "odoo")
ODOO_IMAGE = os.getenv("ODOO_IMAGE", "odoo:18")
# local-path para dev local K3s, ceph-rbd para producción Cloud
STORAGE_CLASS = os.getenv("STORAGE_CLASS", "local-path")
# Middleware namespace = namespace donde se despliegan los middlewares de Traefik
# Los middlewares (odoo-headers, odoo-compress) están en kube-system (ver 02-traefik-config.yaml)
ODOO_HEADERS_MIDDLEWARE = os.getenv("ODOO_HEADERS_MIDDLEWARE", "kube-system-odoo-headers@kubernetescrd")

# ── Per-plan compute resources ───────────────────────────────────────────────
# Each plan tier gets different Odoo workers, CPU, and RAM limits.
# These values are injected into the ConfigMap (odoo.conf) and Deployment.
PLAN_RESOURCES = {
    "starter": {
        "workers": 2, "cron_threads": 1,
        "cpu_req": "100m", "cpu_lim": "500m",
        "mem_req": "512Mi", "mem_lim": "1Gi",
    },
    "pro": {
        "workers": 4, "cron_threads": 1,
        "cpu_req": "250m", "cpu_lim": "1",
        "mem_req": "1Gi", "mem_lim": "2Gi",
    },
    "enterprise": {
        "workers": 8, "cron_threads": 1,
        "cpu_req": "500m", "cpu_lim": "2",
        "mem_req": "2Gi", "mem_lim": "4Gi",
    },
}


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
            "storageClassName": STORAGE_CLASS,
            "resources": {"requests": {"storage": f"{storage_gi}Gi"}},
        },
    }


def secret_manifest(tenant_id: str, db_password: str, admin_password: str, app_admin_password: str) -> dict[str, Any]:
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
            "APP_ADMIN_PASSWORD": b64(app_admin_password),
        },
    }


def configmap_manifest(tenant_id: str, db_password: str, admin_password: str, addons_repos: list = None, plan: str = "starter") -> dict[str, Any]:
    """Odoo config file per tenant — passwords are embedded at provision time."""
    db_name = _dbname(tenant_id)
    addons_repos = addons_repos or []
    import json
    addons_json_str = json.dumps(addons_repos)

    res = PLAN_RESOURCES.get(plan, PLAN_RESOURCES["starter"])

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
workers = {res["workers"]}
max_cron_threads = {res["cron_threads"]}
gevent_port = 8072
proxy_mode = True
without_demo = True
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



def deployment_manifest(tenant_id: str, odoo_version: str = "18.0", custom_image: str | None = None, plan: str = "starter") -> dict[str, Any]:
    pg_user = f"odoo-{tenant_id}"
    db_name = _dbname(tenant_id)
    active_image = custom_image if custom_image else f"odoo:{odoo_version}"
    res = PLAN_RESOURCES.get(plan, PLAN_RESOURCES["starter"])
    # Shared volume mounts and env used by both init and main containers
    _vol_mounts = [
        {"name": "odoo-conf", "mountPath": "/etc/odoo"},
        {"name": "odoo-data", "mountPath": "/var/lib/odoo"},
        {"name": "odoo-extra-addons", "mountPath": "/mnt/extra-addons"},
    ]
    _env = [
        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "odoo-secret", "key": "DB_PASSWORD"}}},
        {"name": "APP_ADMIN_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "odoo-secret", "key": "APP_ADMIN_PASSWORD"}}},
        {"name": "HOST",     "value": POSTGRES_HOST},
        {"name": "PORT",     "value": str(POSTGRES_PORT)},          # 5000 HAProxy primary
        {"name": "USER",     "value": pg_user},
        {"name": "PASSWORD", "valueFrom": {"secretKeyRef": {"name": "odoo-secret", "key": "DB_PASSWORD"}}},
    ]
    # Init env — same port since PgBouncer was removed
    _init_env = [
        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "odoo-secret", "key": "DB_PASSWORD"}}},
        {"name": "APP_ADMIN_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "odoo-secret", "key": "APP_ADMIN_PASSWORD"}}},
        {"name": "HOST",     "value": POSTGRES_HOST},
        {"name": "PORT",     "value": str(POSTGRES_PORT)},          # 5000 HAProxy primary
        {"name": "USER",     "value": pg_user},
        {"name": "PASSWORD", "valueFrom": {"secretKeyRef": {"name": "odoo-secret", "key": "DB_PASSWORD"}}},
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
                    "initContainers": [
                        # 1. Clone custom addons from git repos listed in addons.json
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
                                "' && chown -R 101:101 /mnt/extra-addons"
                            ],
                            "volumeMounts": _vol_mounts,
                            "securityContext": {
                                "runAsUser": 0,
                                "runAsNonRoot": False,
                            },
                        },
                        # 2. Wait for PostgreSQL HA to be reachable before attempting DB init.
                        #    Prevents CrashLoopBackOff caused by DNS timeout on early pod startup.
                        {
                            "name": "wait-for-postgres",
                            "image": "busybox:1.36",
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                f"echo 'Waiting for PostgreSQL HA at {POSTGRES_HOST}:{POSTGRES_PORT}...'; "
                                f"until nc -z {POSTGRES_HOST} {POSTGRES_PORT}; do "
                                "  echo 'PostgreSQL not ready, retrying in 3s...'; sleep 3; "
                                "done; "
                                "echo 'PostgreSQL is ready.'"
                            ],
                        },
                        # 3. Bootstrap DB schema on first start; skip if DB already exists
                        #    (idempotent — safe on CrashLoopBackOff restarts, avoids 45-120s
                        #    re-init overhead that was causing 58-60 restart cycles).
                        {
                            "name": "odoo-init",
                            "image": active_image,
                            "imagePullPolicy": "Always",
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                f"DB_EXISTS=$(PGPASSWORD=$DB_PASSWORD psql "
                                f"-h {POSTGRES_HOST} -p {POSTGRES_PORT} "
                                f"-U {pg_user} -tAc "
                                f"\"SELECT 1 FROM pg_database WHERE datname='{db_name}'\" "
                                f"2>/dev/null || true); "
                                "if [ \"$DB_EXISTS\" = \"1\" ]; then "
                                f"  echo 'DB {db_name} already initialized, skipping --init=base'; "
                                "else "
                                "  echo 'Initializing DB for the first time...'; "
                                "  odoo --config=/etc/odoo/odoo.conf --init=base --stop-after-init && "
                                "  echo \"env.ref('base.user_admin').write({'password': '${APP_ADMIN_PASSWORD}'}); env.cr.commit()\" "
                                "    | odoo shell --config=/etc/odoo/odoo.conf; "
                                "fi"
                            ],
                            "env": _init_env,
                            "volumeMounts": _vol_mounts,
                        },
                    ],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 101,
                        "fsGroup": 101,
                    },
                    "containers": [
                        {
                            "name": "odoo",
                            "image": active_image,
                            "imagePullPolicy": "Always",
                            "args": ["--config=/etc/odoo/odoo.conf"],
                            "ports": [
                                {"containerPort": 8069},
                                {"containerPort": 8072},
                            ],
                            "env": _env,
                            "volumeMounts": _vol_mounts,
                            # startupProbe gives Odoo up to 10 min for the first boot
                            # (module loading + ORM init can take 2-5 min).
                            # Once it passes, livenessProbe takes over with strict timing.
                            "startupProbe": {
                                "httpGet": {"path": "/web/health", "port": 8069},
                                "failureThreshold": 30,
                                "periodSeconds": 20,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/web/health", "port": 8069},
                                "periodSeconds": 30,
                                "failureThreshold": 3,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/web/health", "port": 8069},
                                "periodSeconds": 15,
                                "failureThreshold": 40,
                            },
                            "resources": {
                                "requests": {"cpu": res["cpu_req"], "memory": res["mem_req"]},
                                "limits":   {"cpu": res["cpu_lim"], "memory": res["mem_lim"]},
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


def network_policy_manifest(tenant_id: str) -> dict[str, Any]:
    """Isolate tenant namespace: deny all, allow Traefik for 8069/8072 and Postgres for 5432."""
    ns = _ns(tenant_id)
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "tenant-isolation", "namespace": ns},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {   # Allow Ingress Controller (Traefik)
                    "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                    "ports": [{"protocol": "TCP", "port": 8069}, {"protocol": "TCP", "port": 8072}]
                }
            ],
            "egress": [
                {   # Service postgres en aeisoftware (ClusterIP → Endpoints → HAProxy)
                    "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "aeisoftware"}}}],
                    "ports": [
                        {"protocol": "TCP", "port": POSTGRES_PORT},
                    ]
                },
                {   # Egress directo a red PG HA (192.168.0.0/24)
                    "to": [{"ipBlock": {"cidr": "192.168.0.0/24"}}],
                    "ports": [
                        {"protocol": "TCP", "port": POSTGRES_PORT},
                    ]
                },
                {   # DNS
                    "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]
                },
                {   # GitHub addons HTTPS
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                    "ports": [{"protocol": "TCP", "port": 443}]
                }
            ]
        }
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
    annotations = {
        "traefik.ingress.kubernetes.io/router.entrypoints": "web,websecure",
        "traefik.ingress.kubernetes.io/router.middlewares": ODOO_HEADERS_MIDDLEWARE,
    }

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": "odoo-ingress",
            "namespace": _ns(tenant_id),
            "annotations": annotations,
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


def all_manifests(
    tenant_id: str,
    db_password: str,
    admin_password: str,
    app_admin_password: str,
    storage_gi: int = 10,
    addons_repos: list | None = None,
    odoo_version: str = "18.0",
    custom_image: str | None = None,
    plan: str = "starter",
) -> list[dict]:
    """Return all manifests in apply-order."""
    return [
        namespace_manifest(tenant_id),
        network_policy_manifest(tenant_id),
        pvc_manifest(tenant_id, storage_gi),
        secret_manifest(tenant_id, db_password, admin_password, app_admin_password),
        configmap_manifest(tenant_id, db_password, admin_password, addons_repos, plan=plan),
        deployment_manifest(tenant_id, odoo_version, custom_image, plan=plan),
        service_manifest(tenant_id),
        ingress_manifest(tenant_id),
    ]



# ── helpers ──────────────────────────────────────────────────────────────────
def _ns(tenant_id: str) -> str:
    return f"odoo-{tenant_id}"


def _dbname(tenant_id: str) -> str:
    return f"odoo_{tenant_id}"
