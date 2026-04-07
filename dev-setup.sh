#!/usr/bin/env bash
# =============================================================================
# dev-setup.sh — Bootstrap local K3s dev environment on WSL / Linux
#
# Usage:
#   chmod +x dev-setup.sh
#   DB_PASSWORD="my_db_password" API_KEY="my_api_key" ./dev-setup.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="$REPO_ROOT/k8s"
PORTAL_DIR="$REPO_ROOT/portal"
ADDON_DIR="$REPO_ROOT/odoo_k8s_saas"

PORTAL_IMAGE="saas-portal:dev"
ODOO_IMAGE="odoo:18"

export DB_PASSWORD="${DB_PASSWORD:-DevPass2026!}"
export API_KEY="${API_KEY:-dev-api-key-local}"
export ADMIN_PASSWD="${ADMIN_PASSWD:-admin}"

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Pre-flight Checks ────────────────────────────────────────────────────────
info "Running pre-flight checks..."
if ! command -v docker &>/dev/null; then
  error "Docker is not installed. Please install Docker first: sudo apt install docker.io"
fi

if ! docker ps &>/dev/null; then
  error "Cannot access Docker daemon. Ensure you are in the docker group or run: sudo chmod 666 /var/run/docker.sock"
fi

# ── 0.5. Configure /etc/hosts for local dev (same domains as production) ─────
DEV_HOSTS=("admin.aeisoftware.com" "www.aeisoftware.com" "portal.aeisoftware.com")
info "Ensuring /etc/hosts has local entries for: ${DEV_HOSTS[*]}"
for h in "${DEV_HOSTS[@]}"; do
  if ! grep -q "$h" /etc/hosts; then
    echo "127.0.0.1   $h" | sudo tee -a /etc/hosts > /dev/null
    info "  Added: 127.0.0.1 → $h"
  else
    info "  Already present: $h"
  fi
done

# ── 1. Install K3s ───────────────────────────────────────────────────────────
if ! command -v k3s &>/dev/null; then
  info "Installing K3s …"
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--write-kubeconfig-mode=644" sh -
  sleep 5
else
  info "K3s already installed: $(k3s --version | head -1)"
fi

# Make kubectl use the local kubeconfig
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

info "Waiting for K3s node to be Ready …"
timeout 120 bash -c 'until kubectl get nodes 2>/dev/null | grep -q "Ready"; do sleep 3; done'
kubectl get nodes

# ── 2. Build portal image and import into K3s ────────────────────────────────
info "Building portal image: $PORTAL_IMAGE …"
docker build -t "$PORTAL_IMAGE" "$PORTAL_DIR"

info "Importing portal image into K3s containerd …"
docker save "$PORTAL_IMAGE" | sudo k3s ctr images import -

# ── 3. Create namespaces ─────────────────────────────────────────────────────
info "Applying namespace manifest (and explicitly odoo-admin) …"
kubectl apply -f "$K8S_DIR/00-namespace.yaml"
kubectl create namespace odoo-admin --dry-run=client -o yaml | kubectl apply -f -

# ── 4. Apply dev secrets ─────────────────────────────────────────────────────
info "Applying dynamic dev secrets …"
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: postgres-secret
  namespace: aeisoftware
type: Opaque
stringData:
  POSTGRES_PASSWORD: "${DB_PASSWORD}"
---
apiVersion: v1
kind: Secret
metadata:
  name: portal-secret
  namespace: aeisoftware
type: Opaque
stringData:
  API_KEY: "${API_KEY}"
---
apiVersion: v1
kind: Secret
metadata:
  name: portal-secret
  namespace: odoo-admin
type: Opaque
stringData:
  API_KEY: "${API_KEY}"
---
apiVersion: v1
kind: Secret
metadata:
  name: odoo-admin-secret
  namespace: odoo-admin
type: Opaque
stringData:
  DB_PASSWORD: "${DB_PASSWORD}"
  ADMIN_PASSWD: "${ADMIN_PASSWD}"
---
apiVersion: v1
kind: Secret
metadata:
  name: postgres-secret
  namespace: odoo-admin
type: Opaque
stringData:
  POSTGRES_PASSWORD: "${DB_PASSWORD}"
EOF

# ── 5. Apply Postgres (postgres:16) ──────────────────────────────────────────
info "Applying Postgres 16 …"
kubectl apply -f "$K8S_DIR/02-postgres.yaml"

# ── 6. Apply RBAC ────────────────────────────────────────────────────────────
info "Applying RBAC …"
kubectl apply -f "$K8S_DIR/04-rbac.yaml"

# ── 6.5. Apply Traefik Middlewares ───────────────────────────────────────────
info "Applying Traefik Middlewares (Odoo headers & buffers) …"
kubectl apply -f "$K8S_DIR/03-traefik-middlewares.yaml"

# ── 7. Apply Portal (patched to use local image) ─────────────────────────────
info "Applying portal (local image, imagePullPolicy=Never) …"
kubectl apply -f "$K8S_DIR/05-portal.yaml"
kubectl -n aeisoftware patch deployment portal \
  --type=json \
  -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/image","value":"'"$PORTAL_IMAGE"'"},
    {"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"Never"}
  ]'

# Patch portal API_KEY to match dev value
kubectl -n aeisoftware set env deployment/portal API_KEY="${API_KEY}"

# ── 8. Apply Odoo admin ───────────────────────────────────────────────────────
info "Applying Odoo admin deployment …"
kubectl apply -f "$K8S_DIR/06-odoo-admin.yaml"

# Re-apply dev secrets ONLY over the ones redefined by 06-odoo-admin.yaml
info "Re-applying dev secrets to prevent override by production templates …"
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: odoo-admin-secret
  namespace: odoo-admin
type: Opaque
stringData:
  DB_PASSWORD: "${DB_PASSWORD}"
  ADMIN_PASSWD: "${ADMIN_PASSWD}"
EOF

kubectl -n odoo-admin rollout restart deployment odoo-admin

# ── 9. Expose services locally via LoadBalancer (dev only) ───────────────────────
info "Exposing Odoo and Portal as LoadBalancer services (Standard Ports) …"

kubectl -n aeisoftware expose deployment portal \
  --name=portal-lb \
  --type=LoadBalancer \
  --port=8000 \
  --target-port=8000 \
  --dry-run=client -o yaml | \
  kubectl apply -f - 2>/dev/null || true

# Odoo LoadBalancer requires both 8069 and 8072. We'll use a standard manifest.
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: odoo-lb
  namespace: odoo-admin
spec:
  type: LoadBalancer
  ports:
    - name: http
      port: 8069
      targetPort: 8069
    - name: longpoll
      port: 8072
      targetPort: 8072
  selector:
    app: odoo-admin
EOF

# ── 10. Wait for pods ────────────────────────────────────────────────────────
info "Waiting for Postgres …"
kubectl -n aeisoftware rollout status statefulset/postgres --timeout=120s

info "Waiting for Portal …"
kubectl -n aeisoftware rollout status deployment/portal --timeout=120s

info "Waiting for Odoo admin …"
kubectl -n odoo-admin rollout status deployment/odoo-admin --timeout=180s

# ── 11. Print access info ────────────────────────────────────────────────────
WSL_IP=$(hostname -I | awk '{print $1}')
ODOO_PORT=$(kubectl -n odoo-admin get svc odoo-nodeport -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || echo "?")
PORTAL_PORT=$(kubectl -n aeisoftware get svc portal-nodeport -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || echo "?")

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Local K3s dev environment is ready!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Odoo Admin:   http://${WSL_IP}:${ODOO_PORT}"
echo -e "  Portal API:   http://${WSL_IP}:${PORTAL_PORT}/docs"
echo ""
echo -e "  API key:       ${API_KEY}"
echo -e "  DB password:   ${DB_PASSWORD}"
echo -e "  Admin passwd:  ${ADMIN_PASSWD}"
echo ""
echo -e "  kubectl alias: export KUBECONFIG=/etc/rancher/k3s/k3s.yaml"
echo -e "${GREEN}══════════════════════════════════════════════════════${NC}"
