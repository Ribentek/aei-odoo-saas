# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Odoo SaaS MVP — a multi-tenant SaaS platform for Odoo 18 on Kubernetes (K3s). Automates tenant provisioning, lifecycle management, subscription billing (OCA), and QR payment processing (Banco Mercantil, Bolivia). Production domain: `aeisoftware.com`.

## Branch Strategy

| Branch | Namespace | Domain | Role |
|--------|-----------|--------|------|
| `main` | odoo-stg (staging) | staging.aeisoftware.com | Staging/test |
| `18.0` | odoo-admin | admin.aeisoftware.com | Production |

All changes go to `main` first, test on staging, then merge to `18.0`.

## Architecture

Three layers work together:

1. **Portal API** (`portal/`) — FastAPI service that provisions/manages tenants via Kubernetes API. Runs as a Deployment (2 replicas) in namespace `aeisoftware`. Authenticated by `X-API-Key` header.

2. **Odoo Addons** — Three custom modules installed in the admin Odoo instance:
   - `odoo_k8s_saas` — Core SaaS admin UI. Model `saas.instance` tracks tenants through states: draft → provisioning → ready → suspended → pending_delete → error → deleted. Cron syncs state from K8s every 2 min.
   - `odoo_k8s_saas_subscription` — Bridges OCA subscriptions to SaaS provisioning. Hooks on `stage_id`/`template_id` changes trigger provision/upgrade/suspend. Auto-install addon.
   - `payment_qr_mercantil` — QR payment via Banco Mercantil MC4 API. JWT-cached auth, webhook-driven confirmation, 2s polling on frontend.

3. **Kubernetes Manifests** (`k8s/`) — Applied in lexical order (00-08). Each tenant gets its own namespace (`odoo-{tenant_id}`), PVC, secrets, deployment, service, ingress, and network policy.

**External dependency:** OCA contract/subscription modules included in `external_addons/` (rama 18.0). No longer cloned at deploy time.

## Plan Tiers (portal/k8s_utils/manifests.py)

Starter: 2 workers, 100m-500m CPU, 512Mi-1Gi RAM
Pro: 4 workers, 250m-1 CPU, 1Gi-2Gi RAM
Enterprise: 8 workers, 500m-2 CPU, 2Gi-4Gi RAM

## Cluster Access (SSH)

kubectl no está disponible localmente — todos los comandos se ejecutan vía SSH al nodo control del clúster K3s.

```bash
# Conexión al nodo control (verificar que la clave existe antes de usar)
SSH_CMD="ssh -i .secrets/k3s_rsa -o StrictHostKeyChecking=no ubuntu@10.40.2.158"

# Verificar nodos
$SSH_CMD "kubectl get nodes"
```

| Parámetro | Valor |
|-----------|-------|
| Clave SSH | `.secrets/k3s_rsa` (dentro del repo, gitignored — no se sube a GitHub) |
| Usuario | `ubuntu` |
| Nodo control | `10.40.2.158` (k3s-control-1) |

> **NOTA:** La clave vive en `.secrets/k3s_rsa` (repo-local, en `.gitignore`) en vez de `/tmp/`
> para que no se pierda si se limpia `/tmp` entre sesiones. Si no existe, copiarla desde
> `/tmp/k3s_rsa` (respaldo) o pedirla al usuario.

## Key Deployment Commands

> **IMPORTANTE — Entornos distintos:**
> - **Staging** (`main` branch): namespace `staging`, deployment `odoo-stg`, DB `staging`, portal `portal-stg`
> - **Producción** (`18.0` branch): namespace `odoo-admin`, deployment `odoo-admin`, DB `admin`, portal `portal`
> - NUNCA usar comandos de `odoo-admin` para staging ni viceversa.

> **IMPORTANTE — Addon deployment:** El pod de Odoo tiene un init container que clona el branch correspondiente de GitHub al arrancar. No hay imagen Docker que reconstruir para cambios de addons — solo hacer push y rollout restart.

> **IMPORTANTE — `--no-http`:** Siempre usar `--no-http` al actualizar módulos con `odoo -u` dentro del pod, porque el proceso principal ya ocupa el puerto 8069.

```bash
SSH="ssh -i /tmp/k3s_rsa -o StrictHostKeyChecking=no ubuntu@10.40.2.158"

# Deploy infrastructure (first time)
bash infra/install-k3s.sh
bash infra/install-traefik.sh
cp .secrets.env.example .secrets.env  # edit with real values
bash infra/apply-manifests.sh

# Local dev setup (WSL/Linux)
DB_PASSWORD="..." API_KEY="..." ./dev-setup.sh

# ── STAGING (namespace: staging, branch: main) ──────────────────────────────
# Restart Odoo staging after code changes
$SSH "kubectl rollout restart deployment/odoo-stg -n staging && kubectl rollout status deployment/odoo-stg -n staging"

# Update Odoo module schema on staging
$SSH "POD=\$(kubectl get pod -n staging -l app=odoo-stg -o jsonpath='{.items[0].metadata.name}') && kubectl exec -n staging \$POD -- odoo -u <module_name> -d staging --stop-after-init --no-http"

# Tail staging logs
$SSH "kubectl logs -n staging -l app=odoo-stg -f"

# Restart portal staging
$SSH "kubectl rollout restart deployment/portal-stg -n staging"

# ── PRODUCCIÓN (namespace: odoo-admin, branch: 18.0) ────────────────────────
# Restart Odoo production after code changes
$SSH "kubectl rollout restart deployment/odoo-admin -n odoo-admin && kubectl rollout status deployment/odoo-admin -n odoo-admin"

# Update Odoo module schema on production
$SSH "POD=\$(kubectl get pod -n odoo-admin -l app=odoo-admin -o jsonpath='{.items[0].metadata.name}') && kubectl exec -n odoo-admin \$POD -- odoo -u <module_name> -d admin --stop-after-init --no-http"

# Tail production logs
kubectl logs -n odoo-admin -l app=odoo-admin -f

# Restart portal production
kubectl rollout restart deployment/portal -n aeisoftware

# Portal: build and deploy
docker build -t ghcr.io/ribentek/aei-odoo-saas/portal:main portal/
kubectl rollout restart deployment/portal -n aeisoftware
```

## Portal API (portal/)

- **Entry:** `main.py` → FastAPI app, single router at `/api/v1/instances`
- **Router:** `routers/instances.py` — CRUD + stop/start/upgrade endpoints
- **K8s utils:** `k8s_utils/manifests.py` (manifest generators, PLAN_RESOURCES dict), `k8s_utils/client.py` (K8s SDK wrapper)
- **Dependencies:** `requirements.txt` — fastapi, kubernetes, psycopg2-binary, asyncpg, pydantic, httpx
- **Runs:** uvicorn, 4 workers, port 8000, non-root user `portal`
- **SQL safety:** All DDL uses `psycopg2.sql.Identifier()` — never f-strings for identifiers

## Odoo Addon Conventions

- All addons target Odoo 18.0 (`version: 18.0.x.y.z`)
- Security files at `security/ir.model.access.csv` (and optionally `ir_rules.xml`)
- Views in `views/` as XML, data in `data/` as XML
- Models extend Odoo base via `_inherit` pattern
- Subscription addon depends on `subscription_oca` (OCA module, not in this repo)

## CI/CD (.github/workflows/ci.yaml)

GitHub Actions builds portal Docker image on push to main/18.0. Tags: `:main`, `:18.0`, `:stable` (on 18.0 push), `:$SHA`. Pushes to GHCR. K8s deploy is manual (rollout restart).

## Testing

No automated test suite in the main repo. QA is manual per the test battery in README.md (admin + tenant perspective). OCA modules in `external_addons/` have their own Odoo test framework tests.

## CDN / Cloudflare

`aeisoftware.com` (y subdominios staging/admin/tenants) está detrás del proxy de Cloudflare. Al depurar
errores de frontend/assets, verificar `cf-cache-status` en los headers: el edge puede servir bundles
`/web/assets/*` viejos aunque el servidor esté sano (se sirven con `max-age=1año, immutable` y el hash de
la URL no cambia al recompilar). Tras purgar assets en el servidor o cambiar la imagen de Odoo, purgar
también Cloudflare. Runbook completo en `DEPLOY.md` § "Caché de assets frontend y Cloudflare".

## Reparación de tenants

Nunca reparar tenants con `kubectl` directo (set image, edit deployment...): el `saas.instance` queda
desincronizado y una instancia en `error` no vuelve sola a `ready`. Usar el portal API o las acciones del
módulo SaaS.

## Documentation Wiki

Full documentation (HLD, LLD, runbooks, API reference, QA battery) lives in **`docs/wiki/`** (start at
`docs/wiki/Home.md`) — migrated from the GitHub wiki on 2026-07-10 because private-repo wikis require a
paid GitHub plan to view in the browser (the `aei-odoo-saas.wiki.git` repo still exists but nobody could
read it from the UI). Keep documenting there as plain `.md` files, not on the GitHub wiki. Business
analysis reports live in `docs/` directly.

## Security Patterns

- Secrets via K8s Secrets from `.secrets.env` (never committed)
- Per-tenant NetworkPolicy isolation (default-deny + whitelist)
- Tenant ID validation: regex `^[a-z0-9][a-z0-9\-]{0,46}[a-z0-9]$`
- PodDisruptionBudgets on portal and odoo-admin
- Non-root containers (portal user, odoo UID 101)
- Image pinning (no `:latest` tags)

## Important Files

| Path | Purpose |
|------|---------|
| `infra/apply-manifests.sh` | Main deploy orchestrator (reads .secrets.env, creates namespaces/secrets, applies manifests) |
| `portal/routers/instances.py` | Tenant provisioning API (create, status, upgrade, delete, stop, start) |
| `portal/k8s_utils/manifests.py` | K8s manifest generators + PLAN_RESOURCES |
| `k8s/06-odoo-admin.yaml` | Production Odoo admin deployment (init containers, probes, volumes) |
| `k8s/07-staging.yaml` | Staging environment manifest |
| `odoo_k8s_saas/models/saas_instance.py` | Core tenant model + K8s sync logic |
| `odoo_k8s_saas_subscription/models/sale_subscription.py` | Subscription → provisioning hooks |
| `payment_qr_mercantil/models/payment_transaction.py` | QR payment flow + webhook handler |
| `DEPLOY.md` | Production deployment procedures and diagnostics |
