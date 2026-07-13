# Secrets Management

How credentials are stored, applied, and rotated in the Odoo SaaS MVP.

> **Key Rule:** `k8s/01-secrets.yaml` is a placeholder that ships with empty/placeholder values. **Never commit real credentials.** All live secrets are applied out-of-band via `.secrets.env` + `infra/apply-manifests.sh`.

## Files

| File | Committed? | Purpose |
|:---|:---|:---|
| `k8s/01-secrets.yaml` | ✅ Yes (placeholder only) | Shows the Secret structure; values are empty |
| `.secrets.env` | ❌ No (gitignored) | Your local file with real credentials |
| `.secrets.env.example` | ✅ Yes | Template \u2014 copy to `.secrets.env` and fill in |
| `infra/apply-manifests.sh` | ✅ Yes | Reads `.secrets.env`, renders and applies all secrets + manifests |

## Initial Setup

```bash
# 1. Copy the example file
cp .secrets.env.example .secrets.env

# 2. Fill in real values
nano .secrets.env

# 3. Apply everything
./infra/apply-manifests.sh
```

## .secrets.env Format

```bash
# .secrets.env — NEVER commit this file
POSTGRES_PASSWORD=<strong-random-password>
API_KEY=<strong-random-api-key>
TUNNEL_TOKEN=<cloudflare-tunnel-token>
DB_PASSWORD=<same-as-POSTGRES_PASSWORD>
ADMIN_PASSWD=<odoo-admin-master-password>
```

Generate strong values:

```bash
openssl rand -base64 24   # for POSTGRES_PASSWORD and API_KEY
```

## What apply-manifests.sh Does

1. Reads `.secrets.env`
2. Creates/updates the four cluster Secrets:
   - `aeisoftware/postgres-secret` (`POSTGRES_PASSWORD`)
   - `aeisoftware/portal-secret` (`API_KEY`)
   - `aeisoftware/cloudflare-secret` (`TUNNEL_TOKEN`)
   - `odoo-admin/odoo-admin-secret` (`DB_PASSWORD`, `ADMIN_PASSWD`)
3. Applies all non-secret manifests (`00` through `06`) in order.

It uses `kubectl create secret --dry-run=client -o yaml | kubectl apply -f -` under the hood, so it is idempotent — safe to run multiple times.

## Secret Rotation

### Rotate the Portal API Key

```bash
# 1. Generate a new key
NEW_KEY=$(openssl rand -base64 24)

# 2. Update .secrets.env
sed -i "s|^API_KEY=.*|API_KEY=${NEW_KEY}|" .secrets.env

# 3. Re-apply secrets
./infra/apply-manifests.sh

# 4. Restart both consumers
kubectl -n aeisoftware rollout restart deployment/portal
kubectl -n odoo-admin rollout restart deployment/odoo-admin
```

### Rotate the Postgres Password

```bash
# 1. Generate a new password
NEW_PASS=$(openssl rand -base64 24)

# 2. Update .secrets.env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${NEW_PASS}|" .secrets.env
sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=${NEW_PASS}|" .secrets.env

# 3. Apply to cluster
./infra/apply-manifests.sh

# 4. Set the new password in postgres itself
PG_POD=$(kubectl -n aeisoftware get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}')
kubectl -n aeisoftware exec -it $PG_POD -- \
  psql -U odoo -c "ALTER ROLE odoo WITH PASSWORD '${NEW_PASS}';"

# 5. Restart all consumers
kubectl -n aeisoftware rollout restart deployment/portal
kubectl -n odoo-admin rollout restart deployment/odoo-admin
```

### Rotate the Cloudflare Tunnel Token

```bash
# 1. Generate a new token in Cloudflare Zero Trust dashboard
# 2. Update .secrets.env
sed -i "s|^TUNNEL_TOKEN=.*|TUNNEL_TOKEN=<new-token>|" .secrets.env

# 3. Apply and restart
./infra/apply-manifests.sh
kubectl -n aeisoftware rollout restart deployment/cloudflared
```

## Diagnosing Credential Drift

If secrets in the cluster differ from what `.secrets.env` says:

```bash
# Check postgres password in cluster
kubectl -n aeisoftware get secret postgres-secret \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d && echo

# Check DB_PASSWORD that odoo-admin is using
kubectl -n odoo-admin get secret odoo-admin-secret \
  -o jsonpath='{.data.DB_PASSWORD}' | base64 -d && echo

# Check portal API key
kubectl -n aeisoftware get secret portal-secret \
  -o jsonpath='{.data.API_KEY}' | base64 -d && echo
```

If they differ from `.secrets.env`, run `./infra/apply-manifests.sh` to reconcile and then restart the affected deployments.

See **[Operational Runbook § Troubleshooting: kubectl apply overwrites secrets / DB auth fails](Operational-Runbook.md#troubleshooting-kubectl-apply-overwrites-secrets--db-auth-fails)** for more details on diagnosing drift symptoms.

## CI/CD and Secrets

The GitHub Actions pipeline (`.github/workflows/ci.yaml`) **does not apply secrets** and **does not deploy**. It only:
1. Builds the portal Docker image
2. Pushes it to GHCR (`ghcr.io`)

Deployment to the K3s node is a **manual** step — a human SSHs into the server and runs `kubectl rollout restart`. All secret management is done by humans running `./infra/apply-manifests.sh`. This keeps credentials out of CI environment variables and workflow YAML.

See **[CICD-Pipeline](CICD-Pipeline.md)** for full pipeline details.
