#!/usr/bin/env bash
# Sincroniza external_addons/subscription_oca desde OCA/contract (branch 18.0).
# Fuente canónica: https://github.com/OCA/contract — reemplaza al antiguo
# fork Ribentek/odoo18-oca-contract.
# Idempotente: segunda corrida sin cambios upstream no produce diff.
set -euo pipefail

REPO_URL="https://github.com/OCA/contract.git"
BRANCH="18.0"
MODULE="subscription_oca"

ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
DEST="$ROOT/external_addons/$MODULE"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone --depth=1 --branch "$BRANCH" --filter=blob:none --sparse "$REPO_URL" "$TMP/contract"
git -C "$TMP/contract" sparse-checkout set "$MODULE"

SHA="$(git -C "$TMP/contract" rev-parse HEAD)"

rsync -a --delete "$TMP/contract/$MODULE/" "$DEST/"
echo "$SHA" > "$ROOT/external_addons/OCA_CONTRACT_SHA"

echo "Sincronizado $MODULE desde OCA/contract@$BRANCH ($SHA)"
git -C "$ROOT" status --short external_addons/
