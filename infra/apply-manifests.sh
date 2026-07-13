#!/usr/bin/env bash
# =============================================================================
# infra/apply-manifests.sh
# Apply all K8s manifests in order, injecting secrets from .secrets.env.
#
# Usage (production):
#   ./infra/apply-manifests.sh
#
# Usage (dry-run — shows what would be applied, touches nothing):
#   ./infra/apply-manifests.sh --dry-run
#
# Secrets are read from .secrets.env (gitignored, never committed).
# Copy .secrets.env.example → .secrets.env and fill in real values first.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_FILE="$REPO_ROOT/.secrets.env"
DRY_RUN=false

# ── Argument parsing ─────────────────────────────────────────────────────────
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=true ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

KUBECTL_ARGS=""
if $DRY_RUN; then
  echo "==> DRY RUN mode — no changes will be made to the cluster"
  KUBECTL_ARGS="--dry-run=client"
fi

# ── Load secrets ─────────────────────────────────────────────────────────────
if [[ ! -f "$SECRETS_FILE" ]]; then
  echo ""
  echo "ERROR: $SECRETS_FILE not found."
  echo ""
  echo "  Create it from the example:"
  echo "    cp .secrets.env.example .secrets.env"
  echo "    # edit .secrets.env and fill in real passwords"
  echo ""
  exit 1
fi

# shellcheck source=/dev/null
set -o allexport
source "$SECRETS_FILE"
set +o allexport

# Validate required variables are set and not placeholders
missing=()
for var in DB_PASSWORD ADMIN_PASSWD API_KEY CLOUDFLARE_TUNNEL_TOKEN BACKUP_S3_ACCESS_KEY BACKUP_S3_SECRET_KEY BACKUP_PG_SUPERUSER_PASSWORD SAAS_WEBHOOK_KEY; do
  val="${!var:-}"
  if [[ -z "$val" || "$val" == "change_me" ]]; then
    missing+=("$var")
  fi
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo ""
  echo "ERROR: The following secrets are missing or still set to 'change_me' in $SECRETS_FILE:"
  for v in "${missing[@]}"; do echo "  - $v"; done
  echo ""
  exit 1
fi

# ── Ensure namespaces exist before we try to create secrets in them ──────────
echo "==> Ensuring namespaces exist …"
kubectl create namespace aeisoftware   --dry-run=client -o yaml | kubectl apply $KUBECTL_ARGS -f - 2>/dev/null || true
kubectl create namespace odoo-admin    --dry-run=client -o yaml | kubectl apply $KUBECTL_ARGS -f - 2>/dev/null || true
kubectl create namespace backup-system --dry-run=client -o yaml | kubectl apply $KUBECTL_ARGS -f - 2>/dev/null || true

# ── Ensure odoo-admin PVC exists (kubectl apply silently drops PVCs on 06) ───
echo "==> Ensuring odoo-admin-data PVC exists …"
kubectl get pvc odoo-admin-data -n odoo-admin &>/dev/null || \
  kubectl apply $KUBECTL_ARGS -f - <<'PVCEOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: odoo-admin-data
  namespace: odoo-admin
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ceph-rbd
  resources:
    requests:
      storage: 20Gi
PVCEOF

# ── Apply secrets first (from env vars, never from git files) ────────────────
echo "==> Applying secrets from .secrets.env …"
cat <<EOF | kubectl apply $KUBECTL_ARGS --validate=false -f -
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
  SAAS_WEBHOOK_KEY: "${SAAS_WEBHOOK_KEY}"
---
apiVersion: v1
kind: Secret
metadata:
  name: portal-secret
  namespace: odoo-admin
type: Opaque
stringData:
  API_KEY: "${API_KEY}"
  SAAS_WEBHOOK_KEY: "${SAAS_WEBHOOK_KEY}"
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
EOF

# ── Backup system secrets (backup-system namespace) ──────────────────────────
echo "==> Aplicando backup secrets en namespace backup-system ..."
BACKUP_S3_ENDPOINT="${BACKUP_S3_ENDPOINT:-http://10.40.1.240:7480}"
BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:-pg-backups}"
cat <<EOF | kubectl apply $KUBECTL_ARGS --validate=false -f -
apiVersion: v1
kind: Secret
metadata:
  name: backup-s3-secret
  namespace: backup-system
type: Opaque
stringData:
  AWS_ACCESS_KEY_ID: "${BACKUP_S3_ACCESS_KEY}"
  AWS_SECRET_ACCESS_KEY: "${BACKUP_S3_SECRET_KEY}"
  S3_ENDPOINT: "${BACKUP_S3_ENDPOINT}"
  S3_BUCKET: "${BACKUP_S3_BUCKET}"
---
apiVersion: v1
kind: Secret
metadata:
  name: postgres-superuser-secret
  namespace: backup-system
type: Opaque
stringData:
  POSTGRES_PASSWORD: "${BACKUP_PG_SUPERUSER_PASSWORD}"
EOF

# Cloudflare tunnel token — inyectar en namespace cloudflare (no en aeisoftware)
echo "==> Aplicando cloudflared-token en namespace cloudflare ..."
kubectl create namespace cloudflare --dry-run=client -o yaml | kubectl apply $KUBECTL_ARGS -f - 2>/dev/null || true
cat <<EOF | kubectl apply $KUBECTL_ARGS -f -
apiVersion: v1
kind: Secret
metadata:
  name: cloudflared-token
  namespace: cloudflare
type: Opaque
stringData:
  TUNNEL_TOKEN: "${CLOUDFLARE_TUNNEL_TOKEN}"
EOF

# ── Apply all other manifests (secrets files are deliberately skipped) ────────
echo "==> Applying manifests …"
# Apply backup/ subdirectory manifests (namespace, RBAC, NetworkPolicy, scripts, CronJobs)
for f in "$REPO_ROOT"/k8s/backup/*.yaml; do
  echo "  applying $f …"
  kubectl apply $KUBECTL_ARGS --validate=false -f "$f"
done

for f in "$REPO_ROOT"/k8s/0*.yaml; do
  filename=$(basename "$f")

  # Skip 01-secrets.yaml — it is now a placeholder-only file.
  # All secrets were already applied above from .secrets.env.
  if [[ "$filename" == "01-secrets.yaml" ]]; then
    echo "  skipping $filename (secrets applied from .secrets.env above)"
    continue
  fi

  echo "  applying $f …"
  kubectl apply $KUBECTL_ARGS --validate=false -f "$f"
done

# ── Verificar servicios ──────────────────────────────────────────────────────
if ! $DRY_RUN; then
  echo "==> Verificando endpoints de PostgreSQL HA..."
  kubectl -n aeisoftware get endpoints postgres || true

  echo ""
  echo "==> Todos los manifests aplicados correctamente."
  echo ""
  echo "    Portal:     https://portal.aeisoftware.com"
  echo "    Admin Odoo: https://admin.aeisoftware.com"
  echo "    VIP K3s:    192.168.0.150"
fi
