# High-Level Design (HLD)

## Overview

The Odoo SaaS MVP is a **single-node K3s deployment** that hosts multiple Odoo 18 tenants on one VM. It is the lightweight successor to the full aeisoftware K3s HA cluster — same Cloudflare tunnel routing, no Ceph, no Patroni, no S3.

### Design Goals

| Goal | Approach |
|:---|:---|
| One VM, many tenants | Namespace-per-tenant isolation |
| Zero per-instance DNS work | Wildcard `*.aeisoftware.com` Cloudflare tunnel |
| Simple state storage | K8s objects are the source of truth |
| Minimal dependencies | No S3, no Ceph, no HA database |
| Odoo-driven provisioning | `odoo_k8s_saas` addon calls portal API |

## Component Map

```
┌─────────────────────────────────────────────────────────────────┐
│  Cloudflare                                                       │
│  *.aeisoftware.com CNAME → tunnel                                │
│  Tunnel ingress: * → http://traefik.kube-system.svc.cluster.local│
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTPS
                   ┌───────────▼────────────┐
                   │  cloudflared pod       │
                   │  namespace: aeisoftware│
                   └───────────┬────────────┘
                               │ HTTP (in-cluster)
                   ┌───────────▼────────────┐
                   │  Traefik Ingress        │
                   │  (built-in K3s)         │
                   └──┬──────┬──────┬───────┘
                      │      │      │
             ┌────────▼─┐ ┌──▼──┐ ┌▼────────────┐
             │odoo-acme │ │ ... │ │  aeisoftware │
             │namespace │ │     │ │  namespace   │
             │          │ │     │ │              │
             │ Odoo 18  │ │Odoo │ │ Portal 8000  │
             │ port 8069│ │18   │ │ FastAPI      │
             │ port 8072│ │     │ │ ← K8s API    │
             └────┬─────┘ └──┬──┘ └──────────────┘
                  │          │
             PVC local-path  │        ┌──────────────────┐
             odoo-data       └────────►  postgres         │
                                      │  StatefulSet      │
                                      │  namespace:       │
                                      │  aeisoftware      │
                                      │  50Gi local-path  │
                                      └──────────────────┘
```

## Provisioning Flow

```
Odoo Admin (odoo-admin ns)
  │  operator clicks "Provision" on saas.instance record
  │
  ▼
SaasInstance.action_provision()
  │  POST /api/v1/instances  {tenant_id, plan, storage_gi}
  │  Header: X-API-Key
  │
  ▼
Portal (FastAPI) — routers/instances.py
  │  1. Generate 32-char DB password + admin password
  │  2. Build 7 K8s manifest dicts via k8s_utils/manifests.py
  │  3. Apply each via k8s_utils/client.py (409 = already exists, skip)
  │  4. Return 202 {tenant_id, namespace, url, status:"provisioning"}
  │
  ▼
Kubernetes API (in-cluster ServiceAccount)
  │  Creates: Namespace, PVC, Secret, ConfigMap, Deployment, Service, Ingress
  │
  ▼
Odoo Pod starts in odoo-<tenant_id> namespace
  │  /web/health readiness probe (60s delay, 30 retries × 10s)
  │
  ▼
Cron job (every 2 min) — SaasInstance.action_check_status()
  │  GET /api/v1/instances/{id}
  │  Pod ready? → state = "ready"
```

## Sales-Driven Provisioning

In addition to the manual "Provision" button, instances can be created **automatically** when a customer pays an invoice containing `Odoo-SaaS` products:

```
Sale Order → Invoice → Payment reconciled
  │
  ▼
_compute_payment_state() override detects "paid" transition
  │
  ▼
_saas_check_and_provision() → creates saas.instance → action_provision()
  │
  ▼
(same provisioning flow as above)
```

See **[[Sales Integration]]** for the full details, trigger design, and Odoo 18 gotchas.

## Subscription-Driven Provisioning

For recurring billing, a **bridge module** (`odoo_k8s_saas_subscription`) connects `subscription_oca` to the SaaS provisioning pipeline:

```
Confirm SO (subscribable product)
  → subscription_oca creates sale.subscription (stage: "In Progress")
      → bridge module creates saas.instance linked to subscription
          → subscription cron generates recurring invoices (monthly)
              → payment triggers provisioning (belt-and-suspenders)

Subscription → "Closed"
  → bridge module calls action_delete() → tenant removed
```

The bridge module uses `auto_install: True` — it installs automatically when both `odoo_k8s_saas` and `subscription_oca` are present. If `subscription_oca` is not installed, the base one-time payment flow still works.

See **[[Subscription Integration]]** for the full bridge module architecture and lifecycle hooks.

## Routing Architecture

The wildcard Cloudflare tunnel eliminates all per-tenant DNS API calls:

```
DNS:     *.aeisoftware.com  CNAME  <tunnel-id>.cfargotunnel.com   (proxied)
Tunnel:  *.aeisoftware.com  →      http://traefik.kube-system.svc.cluster.local:80

Per-tenant Ingress (created by portal):
  host: <tenant_id>.aeisoftware.com
  /websocket → odoo:8072
  /          → odoo:8069
```

Traefik matches the `Host` header to the per-namespace Ingress — no extra configuration needed per tenant.

## Database Architecture

All tenants share a single **PostgreSQL 15 StatefulSet** in the `aeisoftware` namespace.

- **One database per tenant:** `odoo_<tenant_id>`
- **Single shared role:** `odoo` (superuser during provisioning)
- **Filtered access:** `odoo.conf` sets `dbfilter = ^odoo_<tenant_id>$` so each Odoo only sees its own database
- **No cross-tenant access** at application level via dbfilter

> **MVP tradeoff:** There is no per-tenant PostgreSQL role/pg_hba isolation. If a tenant pod were compromised it could read other databases using the shared `odoo` credential. The full aeisoftware platform uses per-instance roles. This is a known MVP limitation.

## Security Model

| Layer | Mechanism |
|:---|:---|
| Portal auth | `X-API-Key` header, API_KEY from K8s Secret |
| Cloudflare | Tunnel terminates TLS; no public IP needed |
| K8s RBAC | `saas-portal` ServiceAccount with ClusterRole scoped to needed resources |
| Tenant isolation | Namespace-level network boundary + `dbfilter` |
| Secrets | DB/admin passwords generated with `secrets.choice()`, stored in K8s Secret per namespace |
| Portal container | Runs as non-root user `portal` |

## Namespaces

| Namespace | Contents |
|:---|:---|
| `aeisoftware` | postgres StatefulSet, portal Deployment, cloudflared Deployment, shared Secrets |
| `odoo-admin` | Admin Odoo 18 instance with `odoo_k8s_saas` addon |
| `odoo-<tenant_id>` | One per tenant — 7 K8s objects each |
| `kube-system` | Traefik (K3s built-in), Traefik middleware `odoo-headers` |

## Differences vs Full Platform

| Feature | MVP (this repo) | Full Platform (aeisoftware) |
|:---|:---|:---|
| K8s cluster | Single node K3s | 3-node K3s HA |
| Database | Shared postgres StatefulSet | Patroni HA + per-instance PG roles |
| Storage | `local-path` | Ceph RBD + CephFS |
| Object storage | None | Ceph RGW (S3) |
| Cloudflare routing | Wildcard tunnel (once) | Per-instance CNAME + tunnel rules |
| Addons storage | `emptyDir` + git clone | Ceph CephFS RWX PVC |
| CI/CD | GitHub Actions SSH deploy | GitHub Actions + rolling K8s update |
