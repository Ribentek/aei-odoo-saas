# DAY0 — Install From Scratch (Local K3s)

Complete walkthrough to stand up the Odoo SaaS MVP on a fresh standard Ubuntu 22.04/24.04 VM or local K3s.
(For the Production Cloud environment with Ceph storage, see [Production Cloud Environment](Production-Cloud-Environment.md))

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
git clone https://github.com/Ribentek/aei-odoo-saas.git
cd aei-odoo-saas
```

## 3. Create the Shared Namespace

```bash
kubectl apply -f k8s/00-namespace.yaml
```

Verifies that `aeisoftware` namespace exists.

## 4. Configure Secrets

> **Important:** Secrets are **never stored in git**. `k8s/01-secrets.yaml` is a placeholder only. All real credentials are applied at deploy time via `infra/apply-manifests.sh`.

### 4a. Create your local secrets file

```bash
cp .secrets.env.example .secrets.env
```

Edit `.secrets.env` with real values:

```bash
# .secrets.env — NEVER commit this file (it is gitignored)
POSTGRES_PASSWORD=<strong-random-password>
API_KEY=<strong-random-api-key>
TUNNEL_TOKEN=<cloudflare-tunnel-token>
DB_PASSWORD=<same-as-POSTGRES_PASSWORD>
ADMIN_PASSWD=<odoo-admin-master-password>
```

Generate strong passwords:

```bash
# Generate a strong postgres password
openssl rand -base64 24

# Generate a portal API key
openssl rand -base64 24
```

> The Cloudflare tunnel token is found in Cloudflare Zero Trust → Tunnels → your tunnel → Configure.

### 4b. Apply all manifests (including secrets)

```bash
chmod +x infra/apply-manifests.sh
./infra/apply-manifests.sh
```

This single command:
1. Reads credentials from `.secrets.env`
2. Applies all K8s Secrets (`postgres-secret`, `portal-secret`, `cloudflare-secret`, `odoo-admin-secret`)
3. Applies all other manifests in order

**Dry-run to preview what would be applied:**

```bash
./infra/apply-manifests.sh --dry-run
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

> **Note:** Step 4b already applied `02-postgres.yaml` as part of the full manifest application. This step is only needed if you are applying manifests individually.

## 6. Deploy Cloudflare Tunnel

> Precondition: the tunnel must already be configured in the Cloudflare dashboard to route `*.aeisoftware.com` to `http://traefik.kube-system.svc.cluster.local:80` (or the Traefik ClusterIP).

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
