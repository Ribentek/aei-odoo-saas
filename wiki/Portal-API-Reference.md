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

## POST /api/v1/instances

Provision a new tenant. Applies K8s manifests and returns immediately with `status: provisioning`. The pod starts asynchronously.

**Request Body**

```json
{
  "tenant_id": "acme",
  "plan": "starter",
  "storage_gi": 10
}
```

| Field | Type | Required | Constraints |
|:---|:---|:---|:---|
| `tenant_id` | string | ✓ | Lowercase letters, digits, hyphens only. Becomes subdomain and K8s namespace suffix. |
| `plan` | string | ✓ | `starter`, `pro`, or `enterprise` |
| `storage_gi` | integer | ✓ | PVC size in GB |

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
  "pod_ready": true
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
  -H "X-API-Key: changeme" | jq '{status, pod_ready}'
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
