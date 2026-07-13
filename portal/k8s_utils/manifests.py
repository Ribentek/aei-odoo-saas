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
# Security response headers (HSTS, X-Frame-Options, CSP, Referrer-Policy) — see k8s/03-traefik-middleware.yaml.
# Defaults to the aeisoftware-namespaced copy to match the ODOO_HEADERS_MIDDLEWARE override for tenants.
SECURITY_HEADERS_MIDDLEWARE = os.getenv(
    "SECURITY_HEADERS_MIDDLEWARE", "aeisoftware-security-headers@kubernetescrd"
)
# GitHub PAT for cloning private tenant addon repos (optional — public repos work without it)
GIT_TOKEN = os.getenv("GIT_TOKEN", "")

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


def git_secret_manifest(tenant_id: str, git_token: str) -> dict[str, Any]:
    """Per-tenant secret with GitHub PAT for cloning private addon repos."""
    import base64
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "git-credentials",
            "namespace": _ns(tenant_id),
        },
        "type": "Opaque",
        "data": {
            "GIT_TOKEN": base64.b64encode(git_token.encode()).decode(),
        },
    }


def configmap_manifest(tenant_id: str, db_password: str, admin_password: str, addons_repos: list = None, plan: str = "starter") -> dict[str, Any]:
    """Odoo config file per tenant — passwords are embedded at provision time."""
    db_name = _dbname(tenant_id)
    addons_repos = addons_repos or []
    import json
    addons_json_str = json.dumps(addons_repos)

    res = PLAN_RESOURCES.get(plan, PLAN_RESOURCES["starter"])

    # /mnt/extra-addons is always included: the clone-addons init container
    # (see deployment_manifest) guarantees it's never an empty/invalid addons
    # dir — it drops a placeholder module (installable: False) when no repos
    # are configured. This must stay unconditional so that addon repos added
    # LATER via the "Sync Addons to Instance" button (PATCH /config, which
    # only ever touches addons.json, never re-renders odoo.conf) actually
    # take effect — otherwise Odoo never sees the cloned modules at all
    # regardless of "Update Apps List". See DEPLOY.md incident 2026-07-10.
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



def deployment_manifest(tenant_id: str, odoo_version: str = "18.0", custom_image: str | None = None, plan: str = "starter", install_modules: str = "") -> dict[str, Any]:
    pg_user = f"odoo-{tenant_id}"
    db_name = _dbname(tenant_id)
    active_image = custom_image if custom_image else f"odoo:{odoo_version}"
    res = PLAN_RESOURCES.get(plan, PLAN_RESOURCES["starter"])
    init_modules = f"base,{install_modules}" if install_modules else "base"
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
        # TCP keepalives — prevent HAProxy from dropping idle connections (default 30min timeout).
        # psycopg2/libpq reads PGKEEPALIVES* automatically without any Odoo config changes.
        {"name": "PGKEEPALIVES",          "value": "1"},
        {"name": "PGKEEPALIVES_IDLE",     "value": "60"},   # send keepalive after 60s idle
        {"name": "PGKEEPALIVES_INTERVAL", "value": "10"},   # retry every 10s
        {"name": "PGKEEPALIVES_COUNT",    "value": "5"},    # fail after 5 missed keepalives
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
                                "git_token = os.environ.get(\"GIT_TOKEN\", \"\")\n"
                                "try:\n"
                                "    with open(\"/etc/odoo/addons.json\") as f:\n"
                                "        addons = json.load(f)\n"
                                "except Exception:\n"
                                "    addons = []\n"
                                "os.makedirs(\"/mnt/extra-addons/.repos\", exist_ok=True)\n"
                                "os.makedirs(\"/mnt/extra-addons\", exist_ok=True)\n"
                                "for repo in addons:\n"
                                "    url = repo.get(\"url\")\n"
                                "    branch = repo.get(\"branch\", \"\")\n"
                                "    if not url: continue\n"
                                "    if git_token and url.startswith(\"https://github.com/\"):\n"
                                "        url = url.replace(\"https://\", f\"https://{git_token}@\", 1)\n"
                                "    repo_name = url.rstrip(\"/\").rsplit(\"/\", 1)[-1]\n"
                                "    if repo_name.endswith(\".git\"): repo_name = repo_name[:-4]\n"
                                "    temp_dest = f\"/mnt/extra-addons/.repos/{repo_name}\"\n"
                                "    cmd = [\"git\", \"clone\", \"--depth=1\"]\n"
                                "    if branch:\n"
                                "        cmd.extend([\"-b\", branch])\n"
                                "    cmd.extend([url, temp_dest])\n"
                                "    print(f\"Cloning {url} into {temp_dest}...\")\n"
                                "    if not os.path.exists(temp_dest):\n"
                                "        subprocess.run(cmd, check=True)\n"
                                "    is_single = False\n"
                                "    for filename in [\"__manifest__.py\", \"__openerp__.py\"]:\n"
                                "        if os.path.exists(os.path.join(temp_dest, filename)):\n"
                                "            is_single = True\n"
                                "            break\n"
                                "    if is_single:\n"
                                "        dest = f\"/mnt/extra-addons/{repo_name}\"\n"
                                "        if not os.path.exists(dest):\n"
                                "            os.symlink(temp_dest, dest)\n"
                                "    else:\n"
                                "        for item in os.listdir(temp_dest):\n"
                                "            item_path = os.path.join(temp_dest, item)\n"
                                "            if os.path.isdir(item_path):\n"
                                "                is_sub = False\n"
                                "                for filename in [\"__manifest__.py\", \"__openerp__.py\"]:\n"
                                "                    if os.path.exists(os.path.join(item_path, filename)):\n"
                                "                        is_sub = True\n"
                                "                        break\n"
                                "                if is_sub:\n"
                                "                    dest = f\"/mnt/extra-addons/{item}\"\n"
                                "                    if not os.path.exists(dest):\n"
                                "                        os.symlink(item_path, dest)\n"
                                "' && "
                                "if [ -z \"$(ls -A /mnt/extra-addons 2>/dev/null)\" ]; then "
                                "  mkdir -p /mnt/extra-addons/_placeholder && "
                                "  touch /mnt/extra-addons/_placeholder/__init__.py && "
                                "  printf \"{'name': 'Extra Addons Placeholder', 'version': '1.0', 'installable': False}\\n\" "
                                "    > /mnt/extra-addons/_placeholder/__manifest__.py; "
                                "fi && "
                                "chown -R 101:101 /mnt/extra-addons"
                            ],
                            "env": [
                                {
                                    "name": "GIT_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "git-credentials",
                                            "key": "GIT_TOKEN",
                                            "optional": True,
                                        }
                                    },
                                }
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
                                # Check if Odoo schema exists (not just the DB) by looking for
                                # ir_module_module. A freshly created empty DB would pass the
                                # old "SELECT FROM pg_database" check but still need --init=base.
                                f"DB_INIT=$(PGPASSWORD=$DB_PASSWORD psql "
                                f"-h {POSTGRES_HOST} -p {POSTGRES_PORT} "
                                f"-U {pg_user} -d {db_name} -tAc "
                                "\"SELECT 1 FROM information_schema.tables "
                                "WHERE table_schema='public' AND table_name='ir_module_module'\" "
                                f"2>/dev/null || true); "
                                "if [ \"$DB_INIT\" = \"1\" ]; then "
                                f"  echo 'DB {db_name} already has Odoo schema, skipping --init={init_modules}'; "
                                "else "
                                "  echo 'Initializing Odoo schema for the first time...'; "
                                f"  odoo --config=/etc/odoo/odoo.conf --init={init_modules} --stop-after-init && "
                                "  echo \"env.ref('base.user_admin').write({'password': '${APP_ADMIN_PASSWORD}'}); env.cr.commit()\" "
                                "    | odoo shell --config=/etc/odoo/odoo.conf; "
                                "fi; "
                                # Flush cached asset bundles on every start (not just first boot).
                                # With imagePullPolicy=Always the running Odoo build can legitimately
                                # change between restarts; stale ir.attachment rows compiled by a
                                # different build can register core Owl templates (e.g. mail.Thread)
                                # with mismatched content and break "loadBundle" bundles like
                                # portal.assets_chatter (Missing template errors). Assets recompile
                                # automatically on next request, so this is always safe.
                                f"PGPASSWORD=$DB_PASSWORD psql -h {POSTGRES_HOST} -p {POSTGRES_PORT} "
                                f"-U {pg_user} -d {db_name} "
                                "-c \"DELETE FROM ir_attachment WHERE url LIKE '/web/assets/%'\" "
                                "2>/dev/null || echo 'flush-asset-cache: skipped (DB not ready yet)'; "
                                # Refresh the addon module list on every start (not just first
                                # boot). Needed for "Sync Addons to Instance" (routers/instances.py
                                # patch_instance_config): repos get cloned into /mnt/extra-addons
                                # and the pod restarts, but Odoo never auto-discovers new module
                                # folders on disk — normally someone has to enable developer mode
                                # and click "Update Apps List" by hand. update_list() only
                                # registers/refreshes ir.module.module rows; it does NOT install
                                # anything (a repo can carry many modules — installing the right
                                # one is a deliberate follow-up action, not automatic). See
                                # DEPLOY.md incident 2026-07-10.
                                "echo \"env['ir.module.module'].update_list(); env.cr.commit()\" "
                                "| odoo shell --config=/etc/odoo/odoo.conf --no-http "
                                "|| echo 'update-apps-list: skipped (DB not ready yet)'"
                            ],
                            "env": _init_env,
                            "volumeMounts": _vol_mounts,
                        },
                    ],
                    "affinity": {
                        "nodeAffinity": {
                            # Prefer worker nodes (labelled workload=tenant by 07-join-k3s-workers.sh).
                            # Soft preference — falls back to control-plane if workers are full.
                            "preferredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "weight": 100,
                                    "preference": {
                                        "matchExpressions": [
                                            {
                                                "key": "workload",
                                                "operator": "In",
                                                "values": ["tenant"],
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    },
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
                },
                {   # Allow Portal FastAPI → backup endpoint (prod: aeisoftware, staging: staging)
                    "from": [
                        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "aeisoftware"}}},
                        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "staging"}}},
                    ],
                    "ports": [{"protocol": "TCP", "port": 8069}]
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
        "traefik.ingress.kubernetes.io/router.middlewares": f"{ODOO_HEADERS_MIDDLEWARE},{SECURITY_HEADERS_MIDDLEWARE}",
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


def limitrange_manifest(tenant_id: str) -> dict[str, Any]:
    """Default resource limits for containers that don't declare them explicitly.

    Primarily protects against runaway init containers (clone-addons, wait-for-postgres,
    odoo-init) which have no explicit resources in the Deployment spec.
    The main odoo container has explicit limits and is not affected by these defaults.
    """
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {
            "name": "tenant-limits",
            "namespace": _ns(tenant_id),
        },
        "spec": {
            "limits": [
                {
                    "type": "Container",
                    "default": {
                        "cpu": "500m",
                        "memory": "512Mi",
                    },
                    "defaultRequest": {
                        "cpu": "50m",
                        "memory": "128Mi",
                    },
                }
            ]
        },
    }


def resourcequota_manifest(tenant_id: str, plan: str = "starter") -> dict[str, Any]:
    """Namespace-level resource cap per plan tier.

    Prevents a misconfigured operator or portal bug from creating additional
    pods or PVCs beyond what the plan allows. Values are sized to fit exactly
    1 running Odoo pod (with its init containers) + 1 data PVC.
    """
    # Per-plan namespace caps: generous enough for the workload, tight enough
    # to catch runaway resource creation (extra pods, duplicate PVCs, etc.)
    _quotas = {
        "starter":    {"cpu": "2",   "memory": "4Gi"},
        "pro":        {"cpu": "4",   "memory": "8Gi"},
        "enterprise": {"cpu": "8",   "memory": "16Gi"},
    }
    q = _quotas.get(plan, _quotas["starter"])
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {
            "name": "tenant-quota",
            "namespace": _ns(tenant_id),
        },
        "spec": {
            "hard": {
                # Compute — 4× plan limits to allow init containers + headroom
                "limits.cpu":    q["cpu"],
                "limits.memory": q["memory"],
                # Storage — 1 data PVC + 1 spare for edge cases
                "persistentvolumeclaims": "2",
                # Object count — prevents runaway pod/service creation
                "pods": "5",
                "services": "3",
                "secrets": "10",
                "configmaps": "5",
            }
        },
    }


def pdb_manifest(tenant_id: str) -> dict[str, Any]:
    """PodDisruptionBudget for the tenant Odoo pod.

    minAvailable: 1 means kubectl drain cannot voluntarily evict this pod
    unless a replacement is already running. With replicas=1, this results in
    ALLOWED DISRUPTIONS=0 — the pod is protected from accidental eviction
    during node maintenance. The admin must explicitly scale=0 or delete the
    PDB before draining a node that hosts this tenant.
    """
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {
            "name": "odoo-pdb",
            "namespace": _ns(tenant_id),
        },
        "spec": {
            "minAvailable": 1,
            "selector": {
                "matchLabels": {"app": "odoo"},
            },
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
    git_token: str = "",
    install_modules: str = "",
) -> list[dict]:
    """Return all manifests in apply-order."""
    manifests = [
        namespace_manifest(tenant_id),
        limitrange_manifest(tenant_id),
        resourcequota_manifest(tenant_id, plan=plan),
        network_policy_manifest(tenant_id),
        pvc_manifest(tenant_id, storage_gi),
        secret_manifest(tenant_id, db_password, admin_password, app_admin_password),
        configmap_manifest(tenant_id, db_password, admin_password, addons_repos, plan=plan),
    ]
    if git_token:
        manifests.append(git_secret_manifest(tenant_id, git_token))
    manifests += [
        deployment_manifest(tenant_id, odoo_version, custom_image, plan=plan, install_modules=install_modules),
        service_manifest(tenant_id),
        ingress_manifest(tenant_id),
        pdb_manifest(tenant_id),
    ]
    return manifests



# ── helpers ──────────────────────────────────────────────────────────────────
def _ns(tenant_id: str) -> str:
    return f"odoo-{tenant_id}"


def _dbname(tenant_id: str) -> str:
    return f"odoo_{tenant_id}"
