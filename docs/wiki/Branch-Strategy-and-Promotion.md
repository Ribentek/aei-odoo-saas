# Branch Strategy & Code Promotion

## Overview

The project uses a **two-branch model** with separate staging and production environments in the same K3s cluster:

| Branch | Environment | Used by |
|:---|:---|:---|
| `main` | **Staging** | `odoo-stg` + `portal-stg` (staging.aeisoftware.com) |
| `18.0` | **Production** | `odoo-admin` + `portal` (admin.aeisoftware.com) |

### Cluster Layout

```
┌─────────────────────── K3s Cluster (3 nodes) ──────────────────────┐
│                                                                     │
│  PRODUCTION (branch: 18.0)                                          │
│  ├─ [aeisoftware]  portal (2 replicas) → portal.aeisoftware.com    │
│  ├─ [odoo-admin]   odoo-admin          → admin.aeisoftware.com     │
│  └─ [odoo-*]       tenant pods         → *.aeisoftware.com         │
│                                                                     │
│  STAGING (branch: main)                                             │
│  ├─ [staging]      portal-stg          → portal-stg.aeisoftware.com│
│  ├─ [staging]      odoo-stg            → staging.aeisoftware.com   │
│  └─ [odoo-stg-*]   test tenants        → *.aeisoftware.com         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

DNS: Wildcard `*.aeisoftware.com` covers all subdomains automatically.

## How Code Reaches Each Environment

### Odoo Admin Addons (git clone in initContainer)

| Environment | ConfigMap key | Branch |
|:---|:---|:---|
| Production (`odoo-admin`) | `addon-git-branch: "18.0"` | Stable |
| Staging (`odoo-stg`) | `addon-git-branch: "main"` | Development |

### Portal (Docker image from GHCR)

| Environment | Image tag | CI trigger |
|:---|:---|:---|
| Production (`portal`) | `portal:stable` | Push to `18.0` |
| Staging (`portal-stg`) | `portal:main` | Push to `main` |

CI workflow (`.github/workflows/ci.yaml`) builds:
- Push to `main` → `portal:main` + `portal:<sha>`
- Push to `18.0` → `portal:18.0` + `portal:stable` + `portal:<sha>`

## Daily Development Workflow

### 1. Work on `main`

```bash
git checkout main && git pull
git checkout -b feature/my-change
# ... develop & test locally ...
git checkout main
git merge feature/my-change
git push origin main
```

### 2. Deploy to Staging

```bash
K="ssh -i /tmp/k3s_rsa ubuntu@10.40.2.158 KUBECONFIG=/etc/rancher/k3s/k3s.yaml"

# Restart staging pods (they pull latest main code/image)
$K kubectl -n staging rollout restart deploy/odoo-stg
$K kubectl -n staging rollout restart deploy/portal-stg

# If module fields/views/crons changed, update module:
$K kubectl -n staging exec deploy/odoo-stg -- \
  odoo --config /etc/odoo/odoo.conf \
       --database staging \
       --update odoo_k8s_saas_subscription \
       --stop-after-init --no-http

# Restart again to pick up registry changes
$K kubectl -n staging rollout restart deploy/odoo-stg
```

### 3. Test at staging.aeisoftware.com

- Verify new features work
- Test cron execution
- Create test subscriptions via portal-stg

### 4. Promote to Production

```bash
git checkout 18.0
git pull origin 18.0
git merge main --no-edit
git push origin 18.0
```

### 5. Deploy to Production

```bash
$K kubectl -n odoo-admin rollout restart deploy/odoo-admin
$K kubectl -n aeisoftware rollout restart deploy/portal

# Update module if needed:
$K kubectl -n odoo-admin exec deploy/odoo-admin -- \
  odoo --config /etc/odoo/odoo.conf \
       --database admin \
       --update odoo_k8s_saas_subscription \
       --stop-after-init --no-http

$K kubectl -n odoo-admin rollout restart deploy/odoo-admin
```

## Quick Reference: Full Promotion

```bash
# 1. Push code
cd ~/aei-odoo-saas
git checkout 18.0 && git pull origin 18.0 && git merge main --no-edit && git push origin 18.0 && git checkout main

# 2. Deploy
K="ssh -i /tmp/k3s_rsa ubuntu@10.40.2.158 KUBECONFIG=/etc/rancher/k3s/k3s.yaml"
$K kubectl -n odoo-admin rollout restart deploy/odoo-admin
$K kubectl -n aeisoftware rollout restart deploy/portal
echo "✅ Production updated"
```

## Rollback

```bash
# Revert the merge on 18.0
git checkout 18.0
git revert -m 1 HEAD
git push origin 18.0

# Restart production pods
$K kubectl -n odoo-admin rollout restart deploy/odoo-admin
$K kubectl -n aeisoftware rollout restart deploy/portal
```

## Scaling Staging Down

When not actively testing, free cluster resources:

```bash
$K kubectl -n staging scale deploy --all --replicas=0
```

To bring it back:

```bash
$K kubectl -n staging scale deploy --all --replicas=1
```

## K8s Manifests

| File | What it deploys |
|:---|:---|
| `k8s/05-portal.yaml` | Production portal (image `portal:stable`) |
| `k8s/06-odoo-admin.yaml` | Production odoo-admin (branch `18.0`) |
| `k8s/07-staging.yaml` | Both staging pods + RBAC + ingresses |

## When `-u` (Module Update) Is Required

**Required after changes to:**
- Model fields (new/modified `fields.*`)
- View XML files, security rules
- Cron definitions (`ir_cron.xml`), data files

**NOT required for:**
- Python method logic (method bodies only)
- Static assets (CSS, JS, images)
- Portal FastAPI code (just restart the pod)
