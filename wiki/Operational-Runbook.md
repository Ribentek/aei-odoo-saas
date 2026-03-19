# Operational Runbook

DAY1/DAY2 operations: monitoring, debugging, scaling, and maintenance for the Odoo SaaS MVP.

> **Remote server note**: K3s stores the kubeconfig at `/etc/rancher/k3s/k3s.yaml` (root-only).
> All `kubectl` commands below require either:
> - `KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl ...`, or
> - Running as root: `sudo kubectl ...`, or
> - Copying to your user: `mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown $USER ~/.kube/config`
> After running that copy command once, plain `kubectl` will work for your user.

## Health Checks

```bash
# See all pods across all namespaces
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -A

# See all tenant namespaces
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get namespaces -l managed-by=saas-portal

# Per-tenant pod status
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n odoo-<tenant_id> get pods

# Portal health
curl -s https://portal.aeisoftware.com/healthz | jq .
```

## Viewing Logs

```bash
# Portal logs
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware logs deployment/portal -f

# Postgres logs  (postgres pod is in aeisoftware namespace)
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware logs \
  $(kubectl -n aeisoftware get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}') -f

# cloudflared logs
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware logs deployment/cloudflared -f

# Admin Odoo logs
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n odoo-admin logs deployment/odoo-admin -f

# Tenant Odoo logs
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n odoo-<tenant_id> logs deployment/odoo -f
```

## Provisioning a Tenant (CLI)

```bash
API_KEY=<value>

curl -s -X POST https://portal.aeisoftware.com/api/v1/instances \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"tenant_id":"acme","plan":"starter","storage_gi":10}' | jq .
```

## Checking Tenant Status (CLI)

```bash
curl -s https://portal.aeisoftware.com/api/v1/instances/acme \
  -H "X-API-Key: $API_KEY" | jq .
```

## Deleting a Tenant (CLI)

```bash
curl -s -X DELETE https://portal.aeisoftware.com/api/v1/instances/acme \
  -H "X-API-Key: $API_KEY"
# 204 No Content
```

Verification:

```bash
kubectl get namespace odoo-acme
# Error from server (NotFound)
```

## Recovering a Stuck Provisioning Tenant

Pod stuck in `Pending` or `CrashLoopBackOff`:

```bash
kubectl -n odoo-acme describe pod
kubectl -n odoo-acme logs deployment/odoo --previous
```

Common causes:

| Symptom | Likely cause | Fix |
|:---|:---|:---|
| `0/1 nodes available: 1 Insufficient memory` | VM RAM exhausted | Delete idle tenants or upgrade VM |
| `CreateContainerConfigError` | Missing secret | `kubectl -n odoo-acme get secrets` |
| `CrashLoopBackOff` | Bad `odoo.conf` | Check ConfigMap, correct `db_password` env var |
| `OOMKilled` | Memory limit too low | Increase Odoo deployment limits |
| Readiness probe failing | Odoo database init (normal) | Wait — 60s initial delay, 30 retries |

Re-provision after fixing:

```bash
# Option 1: Delete namespace and re-provision
kubectl delete namespace odoo-acme
# Then POST /api/v1/instances again

# Option 2: From Odoo Admin — set state back to draft and click Provision
```

## Postgres Operations

Postgres runs as a StatefulSet in the `aeisoftware` namespace.

```bash
# Connect to postgres (always prefix with KUBECONFIG on remote servers)
KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  kubectl -n aeisoftware exec -it \
  $(kubectl -n aeisoftware get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}') \
  -- psql -U odoo
```

Once inside the psql shell:

```sql
-- List all databases
\l

-- Check tenant database size
SELECT pg_size_pretty(pg_database_size('odoo_acme'));

-- Drop a stale tenant database (after namespace is already deleted)
DROP DATABASE odoo_acme;

-- Check connection count
SELECT count(*) FROM pg_stat_activity;
```

### Manual Backup

```bash
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PG_POD=$(KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}')
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware exec $PG_POD -- \
  pg_dumpall -U odoo > backup_$TIMESTAMP.sql
```

### Restore to New Cluster

```bash
PG_POD=$(KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}')
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware exec -i $PG_POD -- psql -U odoo < backup_<date>.sql
```

## Scaling

### Add More Tenants

The K3s node can handle approximately **20–30 active tenants** with the default resource limits (100m CPU + 512Mi RAM per tenant) on a 16 GB RAM VM. Leave 20% headroom for the shared stack.

```bash
# Check node resource usage
kubectl top nodes
kubectl top pods -A
```

### Scale Portal Replicas

```bash
kubectl -n aeisoftware scale deployment/portal --replicas=2
```

> Portal is stateless — safe to scale. Ensure idempotent provisioning code handles concurrent requests.

### Increase Tenant Limits

Edit `k8s_utils/manifests.py` to change default limits for new tenants. Existing tenants need a `kubectl edit deployment` in their namespace.

## Updating the Portal

```bash
# After pushing to main branch — manual trigger
kubectl -n aeisoftware rollout restart deployment/portal
kubectl -n aeisoftware rollout status deployment/portal
```

## Updating Admin Odoo / Addon (Remote Server)

The addons are **not** persistent on the remote server. Every pod restart runs an `initContainer` that clones the latest code from GitHub. The update flow is:

**Step 1 — Push your changes to GitHub** (from your local machine):

```bash
git push origin feature/subscription-integration
```

**Step 2 — SSH into the remote server and trigger a rollout restart** (this kills the old pod and starts a new one, which re-clones from GitHub):

```bash
# On the remote server (SSH in first)
ssh user@your-remote-server

# Then trigger the restart
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n odoo-admin rollout restart deployment/odoo-admin

# Watch until the new pod is Running (takes ~60-90 seconds)
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n odoo-admin rollout status deployment/odoo-admin --timeout=180s
```

**Step 3 — Upgrade the module in Odoo UI**:

1. Log in to **https://admin.aeisoftware.com** as admin
2. Go to **Settings → Apps**
3. Search for `odoo_k8s_saas_subscription` (or `odoo_k8s_saas`)
4. Click **Upgrade**

Alternatively, upgrade via CLI (replace `odoo_admin` with your database name):

```bash
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n odoo-admin exec deploy/odoo-admin -- \
  odoo -u odoo_k8s_saas_subscription -d odoo_admin --stop-after-init
```

**Step 4 — Verify** the new pod cloned the right code:

```bash
# Check initContainer logs
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n odoo-admin logs \
  $(kubectl -n odoo-admin get pod -l app=odoo-admin -o jsonpath='{.items[0].metadata.name}') \
  -c copy-addon

# Should show: All addons copied OK + ls output of /mnt/extra-addons
```

## Renewing the Cloudflare Tunnel Token

1. Generate new token in Cloudflare Zero Trust dashboard
2. Update the secret:

```bash
kubectl -n aeisoftware delete secret cloudflare-secret
kubectl -n aeisoftware create secret generic cloudflare-secret \
  --from-literal=TUNNEL_TOKEN=<new_token>
kubectl -n aeisoftware rollout restart deployment/cloudflared
```

## Rotating the Portal API Key

1. Generate new key: `openssl rand -base64 24`
2. Update the secret:

```bash
kubectl -n aeisoftware delete secret portal-secret
kubectl -n aeisoftware create secret generic portal-secret \
  --from-literal=API_KEY=<new_key>
kubectl -n aeisoftware rollout restart deployment/portal
```

3. Update the admin Odoo env var:

```bash
kubectl -n odoo-admin set env deployment/odoo-admin SAAS_PORTAL_KEY=<new_key>
```

## Disk Usage

```bash
# Check node disk
df -h /var/lib/rancher

# Check PVC usage (requires metrics-server)
kubectl describe pvc -n aeisoftware
kubectl describe pvc -A | grep -A5 postgres-data

# Find largest PVCs
kubectl get pvc -A -o custom-columns=\
  'NAMESPACE:.metadata.namespace,NAME:.metadata.name,CAPACITY:.status.capacity.storage'
```

## Emergency: Delete All Tenant Namespaces

```bash
# List all managed namespaces
kubectl get ns -l managed-by=saas-portal -o name

# Delete all (destructive!)
kubectl get ns -l managed-by=saas-portal -o name | xargs kubectl delete
```

## Sales Integration Troubleshooting

### Trigger Not Firing

1. **Check logs** for `SaaS trigger (compute):` messages — if absent, the compute override isn't running
2. **Module upgrade required** after code changes: `Settings → Apps → Upgrade odoo_k8s_saas`
3. **Product category check:** product must be in `Odoo-SaaS` category (or a child of it)
4. **Invoice type:** only `out_invoice` (customer invoice) triggers provisioning

```bash
# Tail admin Odoo logs for sales trigger messages
kubectl -n odoo-admin logs deployment/odoo-admin -f | grep -i "saas"
```

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

See **[[Sales Integration]]** for full details on the trigger design and Odoo 18 `_compute_payment_state()` pattern.

## Subscription Troubleshooting

### Subscription Not Created

1. **Product must be subscribable**: check `subscribable = True` and `subscription_template_id` is set
2. **Module installed?** `subscription_oca` and `odoo_k8s_saas_subscription` must both be installed
3. **Check logs** for subscription creation:

```bash
kubectl -n odoo-admin logs deployment/odoo-admin -f | grep -i "subscription"
```

### Recurring Invoice Not Generated

The subscription cron runs on a schedule. Check if it's active:

```bash
# Check cron status in Odoo
kubectl -n odoo-admin exec -i deploy/odoo-admin -- \
  odoo shell -d odoo_admin --no-http \
  -c "env['ir.cron'].search([('model_id.model','=','sale.subscription')]).read(['name','active','nextcall'])"
```

Trigger manually:
- **Odoo UI**: Settings → Technical → Scheduled Actions → search for subscription cron → Run Manually
- Or wait for next scheduled run

### Instance Not Provisioned on Subscription Stage Change

1. **Check bridge module logs** for `→ In Progress` or `→ Closed` messages
2. **Instance state must match**: provision only triggers for `draft`/`error` instances
3. **Verify linked instance**: `SaaS Instances → search by subscription`

```bash
# Check saas.instance linked to subscription
kubectl -n odoo-admin exec -i statefulset/postgres -- psql -U odoo -d odoo_admin \
  -c "SELECT tenant_id, state, subscription_id FROM saas_instance WHERE subscription_id IS NOT NULL;"
```

### Instance Not Suspended on Subscription Close

1. **Check logs** for `→ Closed: deleting instance` messages
2. **Instance state must be active**: delete only triggers for `draft`/`provisioning`/`ready` instances (not already `deleted`)

### Re-provision Button Not Visible

The **Re-provision Instance** button on the subscription form only appears when:
- Subscription stage is **In Progress** (`in_progress = True`)
- **No active instance** linked (`has_active_instance = False`)

If the button is missing:
1. Check if an instance still exists (even in `error` state — it counts as active)
2. Check if the subscription stage is really "In Progress"
3. Ensure the `odoo_k8s_saas_subscription` module was upgraded after the update

## Common kubectl Aliases

Add to `~/.bashrc` on the K3s node:

```bash
alias kn='kubectl -n'
alias ka='kubectl get pods -A'
alias kl='kubectl logs -f'

# Shortcut to tail portal logs
alias portal-logs='kubectl -n aeisoftware logs deployment/portal -f'

# List all tenant pods
alias tenants='kubectl get pods -l "app=odoo" -A'
```


---

## Troubleshooting: `kubectl apply` Overwrites Secrets / DB Auth Fails

**Symptom:** Odoo pod crash-loops with `FATAL: password authentication failed` or `fe_sendauth: no password supplied`.

**Root cause:** `kubectl apply` on a manifest containing a `Secret` will overwrite the live secret with the YAML value. If the YAML drifted from the real Postgres password, auth breaks.

**Diagnose:**

```bash
# Password Odoo is using
kubectl -n odoo-admin get secret odoo-admin-secret \
  -o jsonpath='{.data.DB_PASSWORD}' | base64 -d && echo

# Actual Postgres superuser password
kubectl -n aeisoftware get secret postgres-secret \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d && echo
```

**Fix:**

```bash
ENCODED=$(echo -n "Aeisoftware2026+" | base64)
kubectl -n odoo-admin patch secret odoo-admin-secret \
  -p "{\"data\":{\"DB_PASSWORD\":\"${ENCODED}\"}}"
kubectl -n odoo-admin rollout restart deployment/odoo-admin
```

**Prevention:** Keep `DB_PASSWORD` in `k8s/06-odoo-admin.yaml` in sync with Postgres and commit to git.

---

## Troubleshooting: Portal Returns 403 From Odoo

**Symptom:** `403 Client Error: Forbidden for url: http://portal.aeisoftware.svc.cluster.local:8000/...`

**Root cause:** `SAAS_PORTAL_KEY` on `odoo-admin` doesn't match `API_KEY` on the portal. Often a one-character typo in the secret (e.g. `AeisoftwarE2026+` vs `Aeisoftware2026+`).

**Diagnose:**

```bash
kubectl -n odoo-admin get secret portal-secret \
  -o jsonpath='{.data.API_KEY}' | base64 -d && echo   # what Odoo sends

kubectl -n aeisoftware get secret portal-secret \
  -o jsonpath='{.data.API_KEY}' | base64 -d && echo   # what the portal accepts
```

**Fix — sync the odoo-admin copy:**

```bash
CORRECT=$(kubectl -n aeisoftware get secret portal-secret \
  -o jsonpath='{.data.API_KEY}')
kubectl -n odoo-admin patch secret portal-secret \
  -p "{\"data\":{\"API_KEY\":\"${CORRECT}\"}}"
kubectl -n odoo-admin rollout restart deployment/odoo-admin
```

---

## Troubleshooting: Portal Returns 500 on Provision

**Symptom:** Availability check passes (200) but provisioning returns 500.

**Check portal logs:**

```bash
kubectl -n aeisoftware logs deployment/portal --tail=40
```

### Sub-case A: `fe_sendauth: no password supplied`

The portal reads `POSTGRES_ADMIN_USER` (default: `postgres`) and `POSTGRES_ADMIN_PASSWORD` (default: `""`). Our superuser is `odoo`, not `postgres`.

**Verify env vars in the running pod:**

```bash
kubectl -n aeisoftware exec deployment/portal -- env | grep POSTGRES_ADMIN
# Expected output:
# POSTGRES_ADMIN_USER=odoo
# POSTGRES_ADMIN_PASSWORD=Aeisoftware2026+
```

If missing, the deployment is stale. Re-apply the manifest:

```bash
cd /tmp/odoo-saas-mvp && git pull origin main
kubectl apply -f k8s/05-portal.yaml
kubectl -n aeisoftware rollout restart deployment/portal
```

> **Note:** Env vars injected by Kubernetes service discovery (`POSTGRES_HOST`, `POSTGRES_PORT`, etc.) are NOT the same as `POSTGRES_ADMIN_USER`/`POSTGRES_ADMIN_PASSWORD`. Use `grep POSTGRES_ADMIN` not `grep -i pg` to confirm the right vars.

### Sub-case B: `role "postgres" does not exist`

Postgres was initialised with `POSTGRES_USER=odoo`; the default `postgres` superuser role doesn't exist. Fix is the same as Sub-case A.

---

## Troubleshooting: Addon Install Fails — XPath Field Not Found

**Symptom:**
```
odoo.tools.convert.ParseError: ...
El elemento '<xpath expr="//field[@name='sale_order_id']">' no puede ser localizado en la vista padre
```

**Root cause:** `odoo_k8s_saas` was updated to add new fields/views, but the database still has the old version. The child module's xpath can't find the field.

**Fix — upgrade the parent module first:**

From the Odoo UI: **Settings → Apps → `odoo_k8s_saas` → Upgrade**

Or via CLI on the remote server:
```bash
ssh -i ~/.ssh/id_25519_aeisoftware ubuntu@10.40.2.198 \
  "sudo kubectl -n odoo-admin exec deployment/odoo-admin -- \
   odoo -d odoo_admin --stop-after-init -u odoo_k8s_saas"
```

Then install `odoo_k8s_saas_subscription` normally.
