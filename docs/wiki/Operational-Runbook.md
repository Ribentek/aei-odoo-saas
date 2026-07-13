# Operational Runbook

> **Navegación:** [← DAY0 Install](DAY0-Install-From-Scratch) | [Secrets Management →](Secrets-Management)

---

## DAY1 — Daily Health Checks

### Cluster Health

```bash
# Node status + resource usage
kubectl top nodes
kubectl get nodes -o wide

# All pods across all namespaces
kubectl get pods -A --sort-by=.metadata.namespace

# Failing pods
kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded
```

### PostgreSQL HA Cluster

```bash
# Patroni cluster health (from any PG node)
ssh ubuntu@10.40.2.174 'curl -s http://127.0.0.1:8008/' | python3 -m json.tool

# Check replication lag
ssh ubuntu@10.40.2.174 'sudo -u postgres psql -h 127.0.0.1 -p 5432 -c "
  SELECT application_name, client_addr, state, sync_state,
         pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS lag_bytes
  FROM pg_stat_replication;"'

# Connection count and capacity (max_connections=800)
ssh ubuntu@10.40.2.174 'sudo -u postgres psql -h 127.0.0.1 -p 5432 -c "
  SELECT count(*) AS active, current_setting('\''max_connections'\'') AS max
  FROM pg_stat_activity;"'
```

### Tenant Instances

```bash
# All tenant namespaces
kubectl get ns -l app=odoo-tenant 2>/dev/null || kubectl get ns | grep odoo-

# Tenant pod status
kubectl get pods -l app=odoo -A

# Instance states in Odoo DB
kubectl -n odoo-admin exec -i deploy/odoo-admin -- \
  odoo shell -d odoo_admin --no-http -c \
  "for i in env['saas.instance'].search([]): print(f'{i.tenant_id}: {i.state}')"
```

---

## DAY2 — Common Operations

### Scale a Tenant

```bash
# Scale tenant to 2 replicas
kubectl -n odoo-<tenant> scale deployment/odoo --replicas=2

# Verify
kubectl -n odoo-<tenant> get pods
```

### Restart a Tenant

```bash
kubectl -n odoo-<tenant> rollout restart deployment/odoo
kubectl -n odoo-<tenant> rollout status deployment/odoo
```

### Force Delete a Stuck Instance

```bash
# 1. Delete from K8s
kubectl delete namespace odoo-<tenant>

# 2. Update state in Odoo DB
kubectl -n odoo-admin exec -i deploy/odoo-admin -- \
  odoo shell -d odoo_admin --no-http -c \
  "inst = env['saas.instance'].search([('tenant_id','=','<tenant>')]); inst.write({'state':'deleted'}); env.cr.commit()"
```

### Portal Logs

```bash
# Production
kubectl -n aeisoftware logs deployment/portal -f --tail=50

# Staging
kubectl -n staging logs deployment/portal-stg -f --tail=50
```

---

## Backups

### pgBackRest — PostgreSQL Backups to Ceph S3

pgBackRest está configurado en los 3 nodos PG con archiving WAL continuo y backups programados a RadosGW (S3-compatible on Ceph).

**Schedule:**
| Type | Frequency | Time | Retention |
|:-----|:----------|:-----|:----------|
| Full | Domingos | 02:00 AM BOT | 4 últimos |
| Differential | Lunes a Sábado | 02:00 AM BOT | 14 últimos |
| WAL Archive | Continuo | — | Hasta 4 full backups atrás |

**Storage:** RadosGW bucket `pg-backups` path `/odoo-saas-ha`, encrypted AES-256-CBC, compressed zstd.

```bash
# Check backup status (run on any PG node, preferably primary)
ssh ubuntu@10.40.2.174 'sudo -u postgres pgbackrest --stanza=odoo-saas info'

# Run manual full backup
ssh ubuntu@10.40.2.174 'sudo -u postgres pgbackrest --stanza=odoo-saas --type=full backup'

# Run manual differential backup
ssh ubuntu@10.40.2.174 'sudo -u postgres pgbackrest --stanza=odoo-saas --type=diff backup'

# Verify stanza health
ssh ubuntu@10.40.2.174 'sudo -u postgres pgbackrest --stanza=odoo-saas check'

# Check WAL archiving stats
ssh ubuntu@10.40.2.174 'sudo -u postgres psql -h 127.0.0.1 -p 5432 -c "
  SELECT archived_count, failed_count, last_archived_wal, last_archived_time
  FROM pg_stat_archiver;"'
```

**Architecture:**
```
PostgreSQL (archive_command) → pgBackRest → stunnel (HTTPS proxy :18480)
                                               → RadosGW (HTTP :7480 on 10.40.1.240)
                                               → Ceph S3 bucket: pg-backups
```

**Restore from backup:**
```bash
# 1. Stop Patroni on all nodes
ssh ubuntu@10.40.2.174 'sudo systemctl stop patroni'
ssh ubuntu@10.40.2.182 'sudo systemctl stop patroni'
ssh ubuntu@10.40.2.193 'sudo systemctl stop patroni'

# 2. Restore on primary
ssh ubuntu@10.40.2.174 'sudo -u postgres pgbackrest --stanza=odoo-saas \
  --delta --type=time "--target=2026-04-10 22:00:00-04" restore'

# 3. Start Patroni on primary first, then replicas
ssh ubuntu@10.40.2.174 'sudo systemctl start patroni'
# Wait for primary to be ready, then start replicas
ssh ubuntu@10.40.2.182 'sudo systemctl start patroni'
ssh ubuntu@10.40.2.193 'sudo systemctl start patroni'
```

### Odoo Admin Backup (DB + Filestore)

```bash
# Manual full backup (DB dump + filestore tar)
kubectl -n odoo-admin exec deploy/odoo-admin -- bash -c '
  BACKUP_DIR="/tmp/backup-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BACKUP_DIR"
  pg_dump -h $DB_HOST -p $DB_PORT -U $DB_USER -d odoo_admin -Fc > "$BACKUP_DIR/odoo_admin.dump"
  tar czf "$BACKUP_DIR/filestore.tar.gz" -C /var/lib/odoo filestore/
  echo "Backup at: $BACKUP_DIR"
  ls -lh "$BACKUP_DIR"'

# Copy backup to local machine
kubectl -n odoo-admin cp odoo-admin-<pod>:/tmp/backup-<timestamp> ./backup-local/
```

---

## Monitoring Stack

### Components

| Component | Namespace | Service | Port | Storage |
|:----------|:----------|:--------|:-----|:--------|
| **Prometheus** | monitoring | `kube-prom-kube-prometheus-prometheus` | 9090 | 20Gi ceph-rbd |
| **Grafana** | monitoring | `kube-prom-grafana` | 80 | 5Gi ceph-rbd |
| **AlertManager** | monitoring | `kube-prom-kube-prometheus-alertmanager` | 9093 | 2Gi ceph-rbd |
| **Loki** | monitoring | `loki` | 3100 | 10Gi ceph-rbd |
| **Promtail** | monitoring | DaemonSet | — | — |

### Access Grafana

```bash
# Option 1: Via Ingress (requires DNS)
# https://grafana.aeisoftware.com
# Login: admin / AeiMonitor2026

# Option 2: Port forward
kubectl -n monitoring port-forward svc/kube-prom-grafana 3000:80
# Then open http://localhost:3000
```

### Access Prometheus

```bash
kubectl -n monitoring port-forward svc/kube-prom-kube-prometheus-prometheus 9090:9090
# Then open http://localhost:9090
```

### Check Monitoring Health

```bash
# All monitoring pods
kubectl -n monitoring get pods

# Check Prometheus targets
kubectl -n monitoring exec prometheus-kube-prom-kube-prometheus-prometheus-0 \
  -c prometheus -- wget -qO- http://localhost:9090/api/v1/targets 2>/dev/null | \
  python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data['data']['activeTargets']:
    j = t['labels'].get('job','?')
    h = t['health']
    s = '✅' if h=='up' else '❌'
    print(f'  {s} {j}: {h} — {t[\"scrapeUrl\"]}')" 2>/dev/null

# Check Grafana datasources
kubectl -n monitoring exec deploy/kube-prom-grafana -c grafana -- \
  curl -s -u admin:AeiMonitor2026 http://localhost:3000/api/datasources | \
  python3 -c "import sys,json;[print(f'  {d[\"name\"]}: {d[\"type\"]}') for d in json.load(sys.stdin)]"
```

### Prometheus Scrape Targets

| Job | Targets | Source |
|:----|:--------|:-------|
| `apiserver` | 3 K3s API servers | ServiceMonitor |
| `kubelet` | 9 (3 nodes × 3 metrics) | ServiceMonitor |
| `node-exporter` | 3 K3s nodes | DaemonSet |
| `node-exporters-pg` | 3 PG nodes (192.168.0.x:9100) | additionalScrapeConfig |
| `postgres-exporters` | 3 PG nodes (192.168.0.x:9187) | additionalScrapeConfig |
| `patroni` | 3 PG nodes (192.168.0.x:8008) | additionalScrapeConfig |
| `kube-state-metrics` | 1 | Deployment |
| `coredns` | 1 | ServiceMonitor |
| `grafana` | 1 | ServiceMonitor |
| `alertmanager` | 2 | ServiceMonitor |
| `prometheus` | 2 | ServiceMonitor |
| `prom-operator` | 1 | ServiceMonitor |

### Useful Prometheus Queries (PromQL)

```promql
# PostgreSQL replication lag
odoo_replication_lag_lag_seconds

# Database sizes (top 10)
topk(10, odoo_database_size_size_bytes)

# Active connections per database
odoo_active_connections_active

# Long running queries (> 1 min)
odoo_long_running_queries_count

# Node CPU usage (K3s)
100 - (avg by(instance)(irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

# Node memory usage
(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100

# Pod restart count
kube_pod_container_status_restarts_total
```

### View Logs (Loki via Grafana)

1. Open Grafana → **Explore** (compass icon)
2. Select datasource: **Loki**
3. Use LogQL:

```logql
# Portal logs
{namespace="aeisoftware", app="portal"}

# Odoo admin logs
{namespace="odoo-admin"}

# Tenant logs
{namespace=~"odoo-.*"}

# Error logs across all namespaces
{namespace=~"aeisoftware|odoo-admin|staging"} |= "ERROR"
```

---

## Stunnel S3 Proxy (PG Nodes)

pgBackRest requiere HTTPS para S3. RadosGW solo soporta HTTP. Stunnel actúa como proxy TLS en cada nodo PG.

```
pgBackRest → HTTPS://127.0.0.1:18480 → stunnel → HTTP://10.40.1.240:7480 (RadosGW)
```

```bash
# Check stunnel status on all PG nodes
for ip in 10.40.2.182 10.40.2.174 10.40.2.193; do
  echo "=== $ip ==="
  ssh ubuntu@$ip 'sudo systemctl is-active stunnel-s3proxy && \
    curl -sk https://127.0.0.1:18480/ -o /dev/null -w "proxy: HTTP %{http_code}\n"'
done

# Restart stunnel if needed
ssh ubuntu@10.40.2.174 'sudo systemctl restart stunnel-s3proxy'
```

---

## Node Management

### PG HA Nodes (SSH via 10.40.2.x)

| Node | SSH | Role | Internal IP |
|:-----|:----|:-----|:-----------|
| aei_postgresql-1 | `ssh ubuntu@10.40.2.182` | replica | 192.168.0.127 |
| aei_postgresql-2 | `ssh ubuntu@10.40.2.174` | **primary** | 192.168.0.186 |
| aei_postgresql-3 | `ssh ubuntu@10.40.2.193` | replica | 192.168.0.226 |

> **Note:** Use `10.40.2.x` for SSH management. The `192.168.0.x` addresses are the internal Ceph/PG network, not reachable from workstations.

### K3s Nodes

| Node | SSH | Internal IP |
|:-----|:----|:-----------|
| k3s-control-1 | `ssh ubuntu@10.40.2.158` | 192.168.0.185 |
| k3s-control-2 | `ssh ubuntu@10.40.2.153` | 192.168.0.211 |
| k3s-control-3 | `ssh ubuntu@10.40.2.159` | 192.168.0.243 |

---

## Troubleshooting

### Instance Not Created (Duplicate Prevention)

If a non-deleted instance already exists for the same sale order, the trigger skips. Check the Odoo UI (SaaS Instances → search by SO name) or:

```bash
kubectl -n odoo-admin exec -i statefulset/postgres -- psql -U odoo -d odoo_admin \
  -c "SELECT tenant_id, state, sale_order_id FROM saas_instance WHERE state != 'deleted';"
```

### Email Not Sent

Email is best-effort — errors are logged but don't block provisioning. Check for the mail template:

```bash
kubectl -n odoo-admin logs deployment/odoo-admin | grep -i "mail_template_instance_provisioned"
```

See **[Sales Integration](Sales-Integration.md)** for full details on the trigger design and Odoo 18 `_compute_payment_state()` pattern.

### Subscription Troubleshooting

See **[Subscription Integration](Subscription-Integration.md)** for subscription-specific issues:
- Subscription not created → check product `subscribable` flag
- Recurring invoice not generated → check cron status
- Instance not provisioned on stage change → check bridge module logs
- Re-provision button not visible → check subscription stage + instance state

### `kubectl apply` Overwrites Secrets / DB Auth Fails

**Symptom:** Odoo pod crash-loops with `FATAL: password authentication failed`.

**Prevention:** Never edit `k8s/01-secrets.yaml` directly. Use:
```bash
./infra/apply-manifests.sh
```

**Diagnose:**
```bash
# Compare passwords
kubectl -n odoo-admin get secret odoo-admin-secret \
  -o jsonpath='{.data.DB_PASSWORD}' | base64 -d && echo
kubectl -n aeisoftware get secret postgres-secret \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d && echo
```

### Portal Returns 403 From Odoo

**Root cause:** `SAAS_PORTAL_KEY` mismatch between `odoo-admin` and `portal`.

```bash
# Compare
kubectl -n odoo-admin get secret portal-secret -o jsonpath='{.data.API_KEY}' | base64 -d && echo
kubectl -n aeisoftware get secret portal-secret -o jsonpath='{.data.API_KEY}' | base64 -d && echo
```

### Portal Returns 500 on Provision

```bash
kubectl -n aeisoftware logs deployment/portal --tail=40
```

Common sub-cases:
- `fe_sendauth: no password supplied` → check `POSTGRES_ADMIN_USER` and `POSTGRES_ADMIN_PASSWORD` env vars
- `role "postgres" does not exist` → superuser is `odoo`, not `postgres`
- RBAC 403 → check `ClusterRole/saas-portal-role` has required resources (namespaces, deployments, services, configmaps, secrets, pvc, networkpolicies, poddisruptionbudgets)

### pgBackRest Archive Failing

```bash
# Check archive stats
ssh ubuntu@10.40.2.174 'sudo -u postgres psql -h 127.0.0.1 -p 5432 -c \
  "SELECT archived_count, failed_count, last_archived_wal, last_failed_wal FROM pg_stat_archiver;"'

# Manual archive test
ssh ubuntu@10.40.2.174 'sudo -u postgres pgbackrest --stanza=odoo-saas \
  --log-level-console=detail archive-push \
  /var/lib/postgresql/16/patroni/pg_wal/$(ls /var/lib/postgresql/16/patroni/pg_wal/ | head -1)'

# Check stunnel proxy
ssh ubuntu@10.40.2.174 'curl -sk https://127.0.0.1:18480/ -o /dev/null -w "HTTP %{http_code}\n"'
```

### Addon Install Fails — XPath Field Not Found

**Fix — upgrade the parent module first:**
```bash
kubectl -n odoo-admin exec deployment/odoo-admin -- \
  odoo -d odoo_admin --stop-after-init -u odoo_k8s_saas
```

---

## Common kubectl Aliases

Add to `~/.bashrc` on the K3s node:

```bash
alias kn='kubectl -n'
alias ka='kubectl get pods -A'
alias kl='kubectl logs -f'
alias portal-logs='kubectl -n aeisoftware logs deployment/portal -f'
alias tenants='kubectl get pods -l "app=odoo" -A'
```
