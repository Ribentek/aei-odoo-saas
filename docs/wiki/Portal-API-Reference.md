# Portal API Reference

Base URL: `https://portal.aeisoftware.com`  
All endpoints under `/api/v1/instances` require the header `X-API-Key: <API_KEY>`.

## Authentication

```
X-API-Key: <value of API_KEY secret>
```

- Returns `403 Forbidden` if the key is missing or wrong.
- The `/healthz` endpoint requires no authentication.

---

## GET /healthz

Liveness probe. No authentication required.

**Response 200**

```json
{"status": "ok"}
```

---

## GET /readyz

Readiness probe. Checks PostgreSQL and Kubernetes API connectivity. No authentication required.

**Response 200** — all dependencies healthy

```json
{"postgres": "ok", "kubernetes": "ok"}
```

**Response 503** — at least one dependency is down (pod removed from load balancer)

```json
{"postgres": "error: connection refused", "kubernetes": "ok"}
```

---

## GET /metrics

Prometheus metrics scrape endpoint. No authentication required.

Returns metrics in Prometheus text format (HTTP request counters, latency histograms, tenant state gauges).

---

## POST /api/v1/instances

Provision a new tenant. Applies K8s manifests and returns immediately with `status: provisioning`. The pod starts asynchronously.

**Request Body**

```json
  "tenant_id": "acme",
  "plan": "starter",
  "storage_gi": 10,
  "odoo_version": "19.0",
  "custom_image": "ghcr.io/Ribentek/custom-odoo-images:19.0",
  "addons_repos": [
    {
      "url": "https://github.com/OCA/server-tools.git",
      "branch": "18.0"
    }
  ]
}
```

| Field | Type | Required | Constraints |
|:---|:---|:---|:---|
| `tenant_id` | string | ✓ | Lowercase letters, digits, hyphens only. Becomes subdomain and K8s namespace suffix. |
| `plan` | string | ✓ | `starter`, `pro`, or `enterprise` |
| `storage_gi` | integer | ✓ | PVC size in GB |
| `odoo_version` | string | | The target Odoo version. Defaults to `18.0`. Can be `17.0`, `18.0`, `19.0`, or `custom`. |
| `custom_image` | string | | Full Docker image URI if using curated tenant builds. Forces `Always` pull policy. |
| `addons_repos` | list | | Optional list of custom git repositories to clone directly into the instance `extra-addons` path. Each dict expects `url` and `branch`. |

**Response 202 — Accepted**

```json
{
  "tenant_id": "acme",
  "namespace": "odoo-acme",
  "url": "https://acme.aeisoftware.com",
  "status": "provisioning"
}
```

**Error Responses**

| Status | Meaning |
|:---|:---|
| `403` | Invalid or missing API key |
| `422` | Validation error (bad `tenant_id` format, missing fields) |
| `500` | K8s API error |

**Example**

```bash
curl -s -X POST https://portal.aeisoftware.com/api/v1/instances \
  -H "Content-Type: application/json" \
  -H "X-API-Key: changeme" \
  -d '{"tenant_id":"acme","plan":"starter","storage_gi":10}' | jq .
```

---

## GET /api/v1/instances/{tenant_id}

Get the current status of a tenant instance.

**Path Parameter**

| Parameter | Description |
|:---|:---|
| `tenant_id` | The slug passed at provisioning time |

**Response 200**

```json
{
  "tenant_id": "acme",
  "namespace": "odoo-acme",
  "url": "https://acme.aeisoftware.com",
  "status": "ready",
  "pod_phase": "Running",
  "pod_ready": true,
  "user_count": 5
}
```

| Field | Values |
|:---|:---|
| `status` | `provisioning` / `ready` / `error` |
| `pod_phase` | `Pending` / `Running` / `Succeeded` / `Failed` / `Unknown` / `NotFound` |
| `pod_ready` | `true` / `false` |

**Status Logic**

```
pod_ready == true  →  status = "ready"
pod_ready == false →  status = "provisioning"
pod_phase == "Failed" → status = "error"
```

**Response 404**

```json
{"detail": "Namespace odoo-acme not found"}
```

**Example**

```bash
curl -s https://portal.aeisoftware.com/api/v1/instances/acme \
  -H "X-API-Key: changeme" | jq '{status, pod_ready, user_count}'
```

---

## GET /api/v1/instances/list

List all active tenant instances by querying K8s namespaces. Excludes `odoo-admin` and `odoo-stg`.

**Query Parameters**

| Parameter | Type | Default | Description |
|:---|:---|:---|:---|
| `user_count` | bool | false | Include live user counts (slower — one DB query per tenant) |

**Response 200**

```json
[
  {
    "tenant_id": "acme",
    "namespace": "odoo-acme",
    "url": "https://acme.aeisoftware.com",
    "status": "ready",
    "user_count": 5
  }
]
```

---

## GET /api/v1/instances/check/{tenant_id}

Check whether a `tenant_id` is available (namespace + DB both absent).

**Response 200 — available**

```json
{"available": true, "tenant_id": "acme", "reasons": []}
```

**Response 200 — taken**

```json
{"available": false, "tenant_id": "acme", "reasons": ["namespace odoo-acme already exists"]}
```

---

## POST /api/v1/instances/{tenant_id}/stop

Suspends a tenant instance by scaling its K8s Deployment down to `0` replicas.

**Response 200 — OK**
```json
{"status": "stopped", "tenant_id": "acme"}
```

---

## POST /api/v1/instances/{tenant_id}/start

Resumes a suspended tenant instance by scaling its K8s Deployment up to `1` replica.

**Response 200 — OK**
```json
{"status": "started", "tenant_id": "acme"}
```

---

## PATCH /api/v1/instances/{tenant_id}/upgrade

Upgrade a running tenant to a different plan tier. Updates CPU/RAM limits in the Deployment and `workers`/`max_cron_threads` in the ConfigMap, then restarts the pod.

**Request Body**

```json
{
  "plan": "pro",
  "storage_gi": 20
}
```

| Field | Type | Required | Description |
|:---|:---|:---|:---|
| `plan` | string | ✓ | `starter`, `pro`, or `enterprise` |
| `storage_gi` | integer | | Optional: expand the PVC to this size (GiB) |

**Response 200**

```json
{"status": "upgrading", "tenant_id": "acme", "plan": "pro"}
```

---

## GET /api/v1/instances/{tenant_id}/backup

Stream a complete Odoo backup (DB + filestore ZIP) for a single tenant. Uses `kubectl exec` to run `dump_db` directly inside the tenant pod, bypassing the `list_db=False` restriction.

**Response 200** — streams a ZIP file

```
Content-Type: application/zip
Content-Disposition: attachment; filename="backup-<tenant_id>-<timestamp>.zip"
```

**Error Responses**

| Status | Meaning |
|:---|:---|
| `404` | Instance not found |
| `422` | Invalid `tenant_id` format |
| `503` | Pod not running or exec failed |

**Example**

```bash
curl -s https://portal.aeisoftware.com/api/v1/instances/acme/backup \
  -H "X-API-Key: $API_KEY" \
  -o acme-backup-$(date +%Y%m%d).zip
```

---

## GET /api/v1/instances/{tenant_id}/config

Returns the configuration details for the tenant, fetching directly from the `odoo-<tenant>-conf` K8s ConfigMap.

**Response 200 — OK**
```json
{
  "odoo_conf": "[options]\ndb_host = postgres...\n",
  "addons_repos": [
    {"url": "...", "branch": "main"}
  ]
}
```

---

## PUT /api/v1/instances/{tenant_id}/config

Updates the `odoo.conf` portion of the tenant's ConfigMap and forcefully restarts the Odoo pod to apply changes.

**Request Body**
```json
{
  "odoo_conf": "[options]\nnew_param = True\n..."
}
```

---

## PATCH /api/v1/instances/{tenant_id}/config

Updates the `addons_repos` portion of the tenant's ConfigMap (persisted as `addons.json`). The instance is restarted, triggering the `copy-addon` container to clone the new repositories into the environment.

**Request Body**
```json
{
  "addons_repos": [
    {"url": "https://github.com/OCA/web", "branch": "18.0"}
  ]
}
```

---

## GET /api/v1/instances/{tenant_id}/logs

Fetches the last 200 lines of live application logs from the tenant's Odoo pod via the Kubernetes API. Requires `pods/log` RBAC permissions to function.

**Response 200 — OK**
```json
{
  "logs": "2026-03-23 12:00:00 INFO odoo: Server started..."
}
```

---

## DELETE /api/v1/instances/{tenant_id}

Delete a tenant and all its Kubernetes resources (namespace cascade-deletes all objects).

**Path Parameter**

| Parameter | Description |
|:---|:---|
| `tenant_id` | The tenant to delete |

**Response 204 — No Content**

Empty body. Namespace deletion is asynchronous — pods may take 30–60 seconds to terminate.

**Response 404**

Returned if the namespace was already deleted — treated as success by the Odoo addon (`action_delete()` accepts 204 and 404).

**Example**

```bash
curl -s -X DELETE https://portal.aeisoftware.com/api/v1/instances/acme \
  -H "X-API-Key: changeme" -o /dev/null -w "%{http_code}"
# 204
```

---

## GET /api/v1/gc/pvs

List PersistentVolumes in `Released` phase for deleted tenant namespaces (orphaned PVs).

**Response 200**

```json
{"count": 2, "pvs": [{"name": "pvc-abc123", "claim_namespace": "odoo-acme", "claim_name": "odoo-data"}]}
```

---

## DELETE /api/v1/gc/pvs

Delete orphaned PersistentVolumes left behind after tenant deletion.

**Query Parameters**

| Parameter | Type | Default | Description |
|:---|:---|:---|:---|
| `dry_run` | bool | false | Preview without deleting |

**Response 200**

```json
{"dry_run": false, "deleted": ["pvc-abc123"], "errors": []}
```

---

## Portal Source Layout

```
portal/
├── main.py                   ← FastAPI app, API key auth, /healthz
├── routers/
│   └── instances.py          ← /api/v1/instances routes
├── k8s_utils/
│   ├── client.py             ← kubernetes Python SDK wrapper
│   └── manifests.py          ← 7-manifest tenant builder
├── requirements.txt
└── Dockerfile
```

## Manifest Generation Details

`k8s_utils/manifests.py::build_tenant_manifests(tenant_id, plan, storage_gi)` returns an ordered list of 7 manifest dicts. Key logic:

- DB password and admin password: `"".join(secrets.choice(alphabet) for _ in range(32))`
- Namespace: `odoo-<tenant_id>`
- DB name: `odoo_<tenant_id>`
- URL: `https://<tenant_id>.<BASE_DOMAIN>`

Calling `apply_manifest()` on each is idempotent — HTTP 409 (already exists) is silently ignored.

## Tenant Defaults: Language + Support User (2026-07-13)

Every NEW tenant's first boot (`odoo-init` init container) now runs a `first_boot.py` script via
`odoo shell` after `--init`:

- **Language `es_BO`**: `--init` runs with `--load-language=es_BO` (translations fall back to
  `es.po` — Odoo ships no `es_BO.po`). The script activates the lang, applies it to all existing
  users/partners, and sets it as `ir.default` for `res.partner.lang` so future users inherit it.
  Override per portal deployment with env `TENANT_DEFAULT_LANG`.
- **Support user**: creates `soporte@aeisoftware.com` (name "Soporte AEI", admin — `base.group_system`)
  with a **per-tenant password generated by the portal** (like `app_admin_password`). The password is
  stored in the tenant's `odoo-secret` (key `SUPPORT_PASSWORD`) and returned once in the create
  response (`support_login` / `support_password`); `action_provision()` logs it as an **internal
  chatter note** (mail.mt_note) on the `saas.instance` — that note is the persistent record on the
  admin side, never emailed to the customer. Override login with portal env `SUPPORT_USER_LOGIN`.
- **Billing**: `_get_user_count()` excludes the support login — it never counts against the plan's
  user limit (business decision: support hours are per instance, user disclosed in ToS).

Existing tenants are NOT retrofitted (first-boot only). To add the support user to an old tenant,
create it manually or re-run the shell snippet inside the tenant pod.
