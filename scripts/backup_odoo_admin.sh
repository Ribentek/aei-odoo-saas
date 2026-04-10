#!/usr/bin/env bash
# ============================================================
# backup_odoo_admin.sh — Full backup (DB + filestore) for odoo-admin
#
# Creates a timestamped .tar.gz containing:
#   - admin.dump     : pg_dump custom format (restore with pg_restore)
#   - filestore/     : full /var/lib/odoo/filestore/admin/ tree
#
# Usage:
#   ./scripts/backup_odoo_admin.sh
#   ./scripts/backup_odoo_admin.sh /custom/backup/dir
#
# Restore:
#   tar xzf odoo_admin_YYYYMMDD_HHMMSS.tar.gz
#   pg_restore -h HOST -p 5002 -U odoo -d admin --clean --if-exists admin.dump
#   kubectl -n odoo-admin cp filestore/admin/ deploy/odoo-admin:/var/lib/odoo/filestore/admin/ -c odoo
#
# Requirements: SSH key at /tmp/k3s_rsa (or set K3S_SSH_KEY env var)
# ============================================================
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────
K3S_HOST="${K3S_HOST:-ubuntu@10.40.2.158}"
K3S_SSH_KEY="${K3S_SSH_KEY:-/tmp/k3s_rsa}"
SSH_OPTS="-i ${K3S_SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=10"

NAMESPACE="odoo-admin"
DB_NAME="admin"
DB_HOST="postgres.aeisoftware.svc.cluster.local"
DB_PORT="5002"
DB_USER="odoo"

BACKUP_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)/backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="odoo_admin_${TIMESTAMP}"
WORK_DIR=$(mktemp -d)

# ── Helpers ────────────────────────────────────────────────────
remote() { ssh ${SSH_OPTS} "${K3S_HOST}" "$@"; }
kube()   { remote "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml && $*"; }

cleanup() { rm -rf "${WORK_DIR}"; }
trap cleanup EXIT

log()  { echo -e "\033[1;34m▸\033[0m $*"; }
ok()   { echo -e "\033[1;32m✓\033[0m $*"; }
fail() { echo -e "\033[1;31m✗\033[0m $*" >&2; exit 1; }

# ── Pre-flight ─────────────────────────────────────────────────
log "Pre-flight checks..."
[ -f "${K3S_SSH_KEY}" ] || fail "SSH key not found: ${K3S_SSH_KEY}"
remote "echo ok" > /dev/null 2>&1 || fail "Cannot SSH to ${K3S_HOST}"
mkdir -p "${BACKUP_DIR}"
ok "SSH connection verified"

# ── Step 1: Dump database via exec into the running Odoo pod ───
# Using exec avoids kubectl run's interactive warnings and
# leverages the pod's existing DB connectivity & env vars.
log "Dumping database '${DB_NAME}'..."

# Get the password from K8s secret
DB_PASSWORD=$(kube "kubectl -n ${NAMESPACE} get secret odoo-admin-secret -o jsonpath='{.data.DB_PASSWORD}' | base64 -d")
[ -n "${DB_PASSWORD}" ] || fail "Could not read DB_PASSWORD from secret"

# Use exec into the odoo pod (has pg_dump via python3-psycopg2 deps)
# But Odoo images may not have pg_dump, so we check first
HAS_PGDUMP=$(kube "kubectl -n ${NAMESPACE} exec deploy/odoo-admin -c odoo -- which pg_dump 2>/dev/null || echo 'no'")

if [ "${HAS_PGDUMP}" != "no" ]; then
    # pg_dump is available in the Odoo container
    kube "kubectl -n ${NAMESPACE} exec deploy/odoo-admin -c odoo -- \
        bash -c 'PGPASSWORD=\"${DB_PASSWORD}\" pg_dump -h ${DB_HOST} -p ${DB_PORT} -U ${DB_USER} -d ${DB_NAME} -Fc'" \
        > "${WORK_DIR}/${DB_NAME}.dump" 2>/dev/null
else
    # Fallback: run a temporary postgres pod on the server, stream back via SSH
    log "  (pg_dump not in Odoo image — using sidecar pod)"
    kube "kubectl -n ${NAMESPACE} exec deploy/odoo-admin -c odoo -- \
        bash -c 'apt-get update -qq && apt-get install -y -qq postgresql-client > /dev/null 2>&1 && \
        PGPASSWORD=\"${DB_PASSWORD}\" pg_dump -h ${DB_HOST} -p ${DB_PORT} -U ${DB_USER} -d ${DB_NAME} -Fc'" \
        > "${WORK_DIR}/${DB_NAME}.dump" 2>/dev/null
fi

DUMP_SIZE=$(du -sh "${WORK_DIR}/${DB_NAME}.dump" | cut -f1)
[ -s "${WORK_DIR}/${DB_NAME}.dump" ] || fail "Database dump is empty!"
ok "Database dump: ${DUMP_SIZE}"

# ── Step 2: Download filestore ─────────────────────────────────
log "Downloading filestore..."
kube "kubectl -n ${NAMESPACE} exec deploy/odoo-admin -c odoo -- \
    tar czf - -C /var/lib/odoo/filestore ${DB_NAME}" \
    > "${WORK_DIR}/filestore.tar.gz" 2>/dev/null

# Extract for clean packaging
mkdir -p "${WORK_DIR}/filestore"
tar xzf "${WORK_DIR}/filestore.tar.gz" -C "${WORK_DIR}/filestore" 2>/dev/null
rm -f "${WORK_DIR}/filestore.tar.gz"

FS_SIZE=$(du -sh "${WORK_DIR}/filestore" | cut -f1)
ok "Filestore: ${FS_SIZE}"

# ── Step 3: Package ───────────────────────────────────────────
log "Creating backup archive..."
ARCHIVE="${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"
tar czf "${ARCHIVE}" -C "${WORK_DIR}" "${DB_NAME}.dump" filestore/

FINAL_SIZE=$(du -sh "${ARCHIVE}" | cut -f1)

# ── Summary ────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ✅ Backup complete                                      ║"
echo "╠══════════════════════════════════════════════════════════╣"
printf "║  %-55s ║\n" "📦 ${ARCHIVE}"
printf "║  %-55s ║\n" "   Total: ${FINAL_SIZE}  (DB: ${DUMP_SIZE} + FS: ${FS_SIZE})"
printf "║  %-55s ║\n" "   Date:  $(date '+%Y-%m-%d %H:%M:%S')"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "── Restore commands ──────────────────────────────────"
echo "  tar xzf ${ARCHIVE}"
echo ""
echo "  # Restore database:"
echo "  pg_restore -h ${DB_HOST} -p ${DB_PORT} -U ${DB_USER} \\"
echo "    -d ${DB_NAME} --clean --if-exists ${DB_NAME}.dump"
echo ""
echo "  # Restore filestore:"
echo "  kubectl -n ${NAMESPACE} cp filestore/${DB_NAME}/ \\"
echo "    deploy/odoo-admin:/var/lib/odoo/filestore/${DB_NAME}/ -c odoo"
