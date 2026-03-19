# DAY0 — Install From Scratch

Complete walkthrough to stand up the Odoo SaaS MVP on a fresh Ubuntu 22.04/24.04 VM.

## Prerequisites

- Ubuntu 22.04+ VM, ≥ 4 vCPU, ≥ 16 GB RAM, ≥ 200 GB disk
- A Cloudflare-managed domain (e.g. `aeisoftware.com`)
- A Cloudflare Tunnel token (Cloudflare Zero Trust → Tunnels)
- `kubectl` available locally (optional, for inspection)

## 1. Install K3s

```bash
curl -sfL https://get.k3s.io | sh -
# Wait for node to be ready
sudo kubectl get nodes
```

Copy kubeconfig for non-root use:

```bash
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $(id -u):$(id -g) ~/.kube/config
chmod 600 ~/.kube/config
export KUBECONFIG=~/.kube/config
```

## 2. Clone the Repository

```bash
cd ~
git clone https://github.com/jpvargassoruco/odoo-saas-mvp.git
cd odoo-saas-mvp
```

## 3. Create the Shared Namespace

```bash
kubectl apply -f k8s/00-namespace.yaml
```

Verifies that `aeisoftware` namespace exists.

## 4. Configure Secrets

Edit `k8s/01-secrets.yaml` — replace base64 placeholders with real values.

```bash
# Generate a strong postgres password
PG_PASS=$(openssl rand -base64 24)
echo -n "$PG_PASS" | base64

# Generate a portal API key
API_KEY=$(openssl rand -base64 24)
echo -n "$API_KEY" | base64
```

Paste the outputs into `k8s/01-secrets.yaml`:
- `postgres-secret.POSTGRES_PASSWORD`
- `portal-secret.API_KEY`

For the Cloudflare tunnel token, paste the token string directly into `cloudflare-secret.TUNNEL_TOKEN` (uses `stringData`, no base64 needed).

```bash
kubectl apply -f k8s/01-secrets.yaml
```

## 5. Deploy PostgreSQL

```bash
kubectl apply -f k8s/02-postgres.yaml
# Wait for postgres to be ready
kubectl -n aeisoftware rollout status statefulset/postgres
```

Verify:

```bash
kubectl -n aeisoftware exec -it statefulset/postgres -- psql -U odoo -c '\l'
```

## 6. Deploy Cloudflare Tunnel

> Precondition: the tunnel must already be configured in the Cloudflare dashboard to route `*.aeisoftware.com` to `http://traefik.aeisoftware.svc.cluster.local:80` (or the Traefik ClusterIP).

```bash
kubectl apply -f k8s/03-cloudflared.yaml
kubectl -n aeisoftware rollout status deployment/cloudflared
```

## 7. Create RBAC (ServiceAccount for Portal)

```bash
kubectl apply -f k8s/04-rbac.yaml
```

This creates:
- `ServiceAccount/saas-portal` in `aeisoftware`
- `ClusterRole/saas-portal-role` with full CRUD on namespaces, deployments, services, ingresses, PVCs, secrets, configmaps
- `ClusterRoleBinding/saas-portal-binding`

## 8. Deploy the Portal

```bash
kubectl apply -f k8s/05-portal.yaml
kubectl -n aeisoftware rollout status deployment/portal
```

Test the portal health:

```bash
kubectl -n aeisoftware port-forward svc/portal 8000:8000 &
curl http://localhost:8000/healthz
# {"status":"ok"}
```

## 9. Deploy Admin Odoo (with SaaS Addon)

```bash
kubectl apply -f k8s/06-odoo-admin.yaml
# This takes 2–3 minutes — init container clones the addon from GitHub
kubectl -n odoo-admin rollout status deployment/odoo-admin
```

Navigate to `https://admin.aeisoftware.com` and complete the Odoo setup wizard.

Once inside Odoo:
1. Go to **Settings → Technical → Modules**
2. Search for **Odoo K8s SaaS**
3. Click Install

### Set SaaS Portal Environment Variables

The admin Odoo needs to reach the portal. Either set env vars in `k8s/06-odoo-admin.yaml` or via `kubectl`:

```bash
kubectl -n odoo-admin set env deployment/odoo-admin \
  SAAS_PORTAL_URL=http://portal.aeisoftware.svc.cluster.local:8000 \
  SAAS_PORTAL_KEY=<your-api-key>
```

## 10. Provision the First Tenant (Smoke Test)

```bash
# Get the API key you set earlier
API_KEY=<your-api-key>

curl -s -X POST https://portal.aeisoftware.com/api/v1/instances \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"tenant_id":"demo","plan":"starter","storage_gi":10}' | jq .
```

Expected response:

```json
{
  "tenant_id": "demo",
  "namespace": "odoo-demo",
  "url": "https://demo.aeisoftware.com",
  "status": "provisioning"
}
```

Poll status:

```bash
curl -s https://portal.aeisoftware.com/api/v1/instances/demo \
  -H "X-API-Key: $API_KEY" | jq .status
```

Wait for `"ready"` (typically 2–5 minutes on first Odoo start).

## 11. Cloudflare Tunnel Configuration Reference

In Cloudflare Zero Trust dashboard:

| Setting | Value |
|:---|:---|
| Tunnel type | `cloudflared` |
| Public hostname | `*.aeisoftware.com` |
| Service | `http://traefik.kube-system.svc.cluster.local:80` |

> Use the cluster-internal Traefik Service address. K3s deploys Traefik in `kube-system`.

## Validation Checklist

- [ ] `kubectl -n aeisoftware get pods` — all Running
- [ ] `kubectl -n odoo-admin get pods` — Running/Ready
- [ ] `https://portal.aeisoftware.com/healthz` → `{"status":"ok"}`
- [ ] `https://admin.aeisoftware.com` → Odoo login page
- [ ] Demo tenant `https://demo.aeisoftware.com` → Odoo database setup page
- [ ] `kubectl -n odoo-demo get pods` — `1/1 Running`
