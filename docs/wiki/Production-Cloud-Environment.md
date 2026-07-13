# Production Cloud Environment (Cloud K3s)

This document describes the architectural differences and necessary configuration when deploying the Odoo SaaS MVP to the production environment, which currently runs on a cloud-hosted Kubernetes cluster (OpenStack / K3s) with Ceph storage.

## Server Access

| Item | Value |
|:---|:---|
| Host | `10.40.2.158` (internal VPN/network) |
| User | `ubuntu` |
| SSH Key | `/tmp/k3s_rsa` (local) |
| K3s Config | `/etc/rancher/k3s/k3s.yaml` |

```bash
# Quick access
ssh -i /tmp/k3s_rsa ubuntu@10.40.2.158

# Run kubectl remotely
ssh -i /tmp/k3s_rsa ubuntu@10.40.2.158 \
  'KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -A'
```

> **Note:** The server is on the `10.40.x.x` network which requires VPN access or being on the same internal network.

## Architectural Differences

Unlike the local K3s setup (which relies on `local-path` for simplicity), the production environment utilizes a distributed block storage backend, **Ceph-RBD**.

### Key Changes

| Area | Local Dev | Production |
|:---|:---|:---|
| Network | localhost / WSL bridge | `10.40.x.x` internal VPN |
| Storage | `local-path` | `ceph-rbd` |
| PostgreSQL Port | `5432` (standard) | **`5000`** (HAProxy primary) |
| Security Context | None (commented out in dev) | `runAsUser: 100`, `fsGroup: 101` |
| PVC Permissions | auto (local-path handles it) | Ceph volumes are `root:root` — requires explicit `fsGroup` |
| Manifest Directory | `k8s/` | `k8s/prod/` |

### PostgreSQL Port via HAProxy

Production uses `db_port = 5000` (HAProxy) instead of the standard `5432`. This routes through HAProxy which automatically follows Patroni failover to the current primary. Set in:
- `k8s/05-portal.yaml` → env var `POSTGRES_PORT`
- `k8s/prod/06-odoo-admin-cloud.yaml` → `odoo.conf.tmpl` ConfigMap
- `portal/k8s_utils/manifests.py` → `POSTGRES_PORT` default

All `odoo.conf` templates and portal API calls use port 5000.

## Deploying

A separate folder has been established specifically for production overrides.
Whenever applying the main Admin layer in production, use the `k8s/prod` declarations instead of the base `k8s/` files:

```bash
# Apply the production admin manifest
kubectl apply -f k8s/prod/06-odoo-admin-cloud.yaml

# Apply production network policies
kubectl apply -f k8s/prod/07-network-policies.yaml
```

### Production Manifest Files

| File | Contents | Difference vs Dev |
|:---|:---|:---|
| `k8s/prod/06-odoo-admin-cloud.yaml` | Full admin Odoo stack | `ceph-rbd` storage, `securityContext`, port `5000` in odoo.conf |
| `k8s/prod/07-network-policies.yaml` | NetworkPolicies + CiliumNetworkPolicy | Egress PG HA ports 5000-5001, K8s API via CiliumNetworkPolicy |

### Understanding `06-odoo-admin-cloud.yaml` Changes

Inside this file, you will notice these explicitly enforced settings compared to the standard file:

```yaml
  # Forces deployment to utilize cluster networked Ceph block storage
  storageClassName: ceph-rbd
```

```yaml
      # Dictates Kubelet to chgrp the mounted block storage natively
      securityContext:
        runAsNonRoot: true
        runAsUser: 100
        fsGroup: 101
```

```yaml
      # PostgreSQL via HAProxy primary
      db_port = 5000
```

> **Warning:** Never delete and re-apply PersistentVolumeClaims (PVCs) arbitrarily in production. Deleting a PVC bound to Ceph-RBD will permanently destroy the associated Odoo filestore (session files and user attachments).

## Database — PostgreSQL HA Cluster

### Architecture

Production uses an **external PostgreSQL HA cluster** (Patroni + HAProxy) that is **NOT running inside K8s**. It is connected to the cluster via a headless Service with manual Endpoints:

```
┌─────────────────────────────────────────────────┐
│  K8s Cluster (K3s on 10.40.2.158)               │
│                                                   │
│  svc/postgres (headless, ns: aeisoftware)        │
│    port 5000 → primary (read-write)              │
│    port 5001 → replica (read-only)               │  ← all traffic via HAProxy
│                                                   │
│  Endpoints:                                       │
│    192.168.0.226:5000/5001                        │
│    192.168.0.127:5000/5001                        │
│    192.168.0.186:5000/5001                        │
└──────────────────┬──────────────────────────────┘
                   │ (HAProxy / Patroni)
    ┌──────────────┼──────────────┐
    │              │              │
┌───▼───┐   ┌─────▼──┐   ┌──────▼─┐
│ PG Node│   │ PG Node│   │ PG Node│
│ .226   │   │ .127   │   │ .186   │
│ Patroni│   │ Patroni│   │ Patroni│
└────────┘   └────────┘   └────────┘
```

### Key Details

| Item | Value |
|:---|:---|
| PostgreSQL Version | 16 |
| HA Solution | Patroni (automatic failover via etcd) |
| Connection Model | Direct via HAProxy (no connection pooler) |
| max_connections | **800** (supports ~257 tenants without pooling) |
| Primary (r/w) | Port 5000 (all Odoo traffic) |
| Replica (r/o) | Port 5001 (reserved for future read scaling) |
| LISTEN/NOTIFY | ✅ Natively supported (longpolling, chat, Discuss) |
| Nodes | `192.168.0.226`, `192.168.0.127`, `192.168.0.186` |
| K8s Service | `postgres.aeisoftware.svc.cluster.local` (headless, no pod selector) |
| Odoo workers connect to | Port **5000** (HAProxy → Patroni primary) |
| Portal DDL operations | Port **5000** (same path — no bypass needed) |
| DB User (shared admin) | `odoo` (CREATEROLE, CREATEDB) |
| Tenant isolation | Per-tenant role + database (e.g. `odoo-acme`, `odoo_acme`) |

### Why No PgBouncer?

PgBouncer was removed from the architecture (2026-04-11) because:

1. **LISTEN/NOTIFY incompatible** with `pool_mode=transaction` → broke longpolling (chat, Discuss, real-time notifications)
2. **DDL blocked** — `CREATE ROLE`, `CREATE DATABASE` required a separate bypass connection
3. **Added complexity** — extra component to configure, monitor, authenticate (SCRAM hashes, `user_lookup()`)
4. **Unnecessary at current scale** — with `max_connections=800` and ~3 connections per tenant, PostgreSQL handles 250+ tenants directly

PgBouncer remains installed (but **disabled**: `systemctl disable pgbouncer`) on all 3 PG nodes. It can be re-enabled if the tenant count exceeds ~250 and connection pressure becomes an issue.

### Connection Capacity

```
max_connections = 800
 - superuser_reserved       = 10
 - replicator (2 replicas)  =  2
 - pgbackrest               =  1
 - postgres_exporter         =  5
 - odoo-admin (2w + 1c)     =  3
 - staging (2w + 1c)        =  3
 - reserva                  = 10
 ────────────────────────────────
 Available for tenants       = 766
 Each tenant: 2w + 1c        = 3 connections
 Max tenants                 ≈ 255
```

### Port Summary

| Port | What | Used by |
|:---|:---|:---|
| 5000 | HAProxy → Primary (read-write) | All Odoo traffic, portal DDL, init containers |
| 5001 | HAProxy → Replicas (read-only) | Reserved for future read scaling |

### K8s Service Definition

The `postgres` Service in the `aeisoftware` namespace is a **headless service without a pod selector**. It uses manually-defined `Endpoints` that point to the external Patroni nodes:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: aeisoftware
  annotations:
    description: "Proxy al clúster PostgreSQL HA externo via HAProxy"
spec:
  clusterIP: None
  ports:
    - name: primary
      port: 5000
    - name: replica
      port: 5001
```

### odoo.conf for Admin

The admin ConfigMap in production (`odoo-admin-conf`) contains:

```ini
[options]
db_host = postgres.aeisoftware.svc.cluster.local
db_port = 5000
db_user = odoo
db_password = REPLACE_DB_PASSWORD
admin_passwd = REPLACE_ADMIN_PASSWD
dbfilter = ^admin$
list_db = True
addons_path = /mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons
data_dir = /var/lib/odoo
workers = 2
max_cron_threads = 1
gevent_port = 8072
proxy_mode = True
limit_time_cpu = 1200
limit_time_real = 2400
```

Key: `REPLACE_DB_PASSWORD` and `REPLACE_ADMIN_PASSWD` are substituted at runtime by the `render-config` init container using values from the `odoo-admin-secret` K8s Secret.

## Backup & Restore

### pgBackRest (Production Backups)

Automated backups via pgBackRest to Ceph S3 (RadosGW):

| Schedule | Type | Retention |
|:---|:---|:---|
| Sundays 2:00 AM | Full backup | 4 full |
| Mon–Sat 2:00 AM | Differential | 14 diff |
| Continuous | WAL archiving | Unlimited (within retention window) |

Encryption: AES-256-CBC. Compression: zstd.

See [Operational Runbook](Operational-Runbook.md) for full backup/restore commands.

### Manual Backup via Odoo UI

Via the Odoo web interface:
1. Go to `https://admin.aeisoftware.com/web/database/manager`
2. Click "Backup" on the `admin` database
3. Choose "zip" format (includes SQL dump + filestore)

### Restoring

See [Operational Runbook](Operational-Runbook.md) § Backup / Restore for the full procedure including PITR (Point-in-Time Recovery).

**Common Issues:**
1. **`dbfilter = ^admin$`** — The backup database name must be `admin` (matching the filter)
2. **`workers = 2`** — Worker timeout may kill long-running restore operations. Consider setting `workers = 0` temporarily
3. **Disk space** — Ensure the `odoo-admin-data` PVC (20Gi) has enough room for the extracted backup
4. **Permissions** — `securityContext` must set `fsGroup: 101` for Ceph volumes

## Monitoring

Full observability stack deployed in the `monitoring` namespace:

| Component | Function |
|:---|:---|
| Prometheus | Metrics (15d retention, 20Gi) — 32 scrape targets |
| Grafana | Dashboards — `grafana.aeisoftware.com` (admin/AeiMonitor2026) |
| Loki + Promtail | Centralized log aggregation |
| AlertManager | Alert routing (2Gi) |
| postgres_exporter | PG metrics (database sizes, connections, replication lag) |
| node_exporter | System metrics on K3s + PG HA nodes |

See [Operational Runbook](Operational-Runbook.md) for PromQL queries, LogQL examples, and Grafana access.

## Troubleshooting

### Check Pod Status
```bash
ssh -i /tmp/k3s_rsa ubuntu@10.40.2.158 \
  'KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -n odoo-admin -o wide'
```

### View Logs
```bash
ssh -i /tmp/k3s_rsa ubuntu@10.40.2.158 \
  'KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl logs -n odoo-admin deployment/odoo-admin --tail=100'
```

### Check PVC Usage
```bash
ssh -i /tmp/k3s_rsa ubuntu@10.40.2.158 \
  'KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl exec -n odoo-admin deployment/odoo-admin -- df -h /var/lib/odoo'
```
