# CI/CD Pipeline

The pipeline builds and publishes the portal Docker image from the `main` branch and redeploys it on the K3s node via SSH.

> **Note:** As of the current repository state the GitHub Actions workflow YAML is not yet committed. This page describes the intended pipeline based on the project structure.

## Pipeline Overview

```
git push → main
      │
      ▼
GitHub Actions
  ┌─────────────────────────────────────────────┐
  │  job: build-and-deploy                       │
  │                                              │
  │  1. checkout                                 │
  │  2. docker buildx build portal/Dockerfile    │
  │  3. push → ghcr.io/.../portal:latest         │
  │             + portal:<sha>                   │
  │  4. SSH into K3s node                        │
  │  5. kubectl rollout restart deployment/portal│
  └─────────────────────────────────────────────┘
```

## Container Registry

| Field | Value |
|:---|:---|
| Registry | `ghcr.io` (GitHub Container Registry) |
| Image | `ghcr.io/jpvargassoruco/odoo-saas-mvp/portal` |
| Tags | `latest` (rolling) + `<git-sha>` (immutable) |
| Pull policy | `Always` (set in `k8s/05-portal.yaml`) |

The portal Deployment uses `imagePullPolicy: Always`, so a `rollout restart` pulls the new image even if the tag is the same.

## Recommended Workflow YAML

Create `.github/workflows/deploy.yml`:

```yaml
name: Build & Deploy Portal

on:
  push:
    branches: [main]
    paths:
      - "portal/**"
      - "k8s/**"
      - ".github/workflows/deploy.yml"

env:
  REGISTRY: ghcr.io
  IMAGE: ghcr.io/${{ github.repository_owner }}/odoo-saas-mvp/portal

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push portal image
        uses: docker/build-push-action@v5
        with:
          context: portal/
          push: true
          tags: |
            ${{ env.IMAGE }}:latest
            ${{ env.IMAGE }}:${{ github.sha }}

      - name: Deploy to K3s node
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.K3S_HOST }}
          username: ${{ secrets.K3S_USER }}
          key: ${{ secrets.K3S_SSH_KEY }}
          script: |
            kubectl -n aeisoftware rollout restart deployment/portal
            kubectl -n aeisoftware rollout status deployment/portal --timeout=120s
```

## Required GitHub Secrets

| Secret | Description |
|:---|:---|
| `K3S_HOST` | Public IP or DNS of the K3s node |
| `K3S_USER` | SSH user (e.g. `ubuntu`) |
| `K3S_SSH_KEY` | Private SSH key with access to the node |
| `GITHUB_TOKEN` | Auto-provided by GitHub Actions — used for GHCR auth |

Set secrets in: **Repository → Settings → Secrets and variables → Actions**

## First-Time Setup: SSH Key

On the K3s node, add the deploy key to `~/.ssh/authorized_keys`:

```bash
# On your local machine
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/odoo_saas_deploy

# Copy the contents of ~/.ssh/odoo_saas_deploy.pub to the K3s node
ssh ubuntu@<K3S_HOST> "echo '<pubkey>' >> ~/.ssh/authorized_keys"

# Add the private key (~/.ssh/odoo_saas_deploy) to GitHub Secrets as K3S_SSH_KEY
```

## Kubernetes Auth on the Node

The deploy step needs `kubectl` access. On the K3s node:

```bash
# Ensure kubectl points to the cluster (already set up in Day 0)
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes   # sanity check
```

## Odoo Addon Deployment

The `odoo_k8s_saas` addon is deployed differently — the admin Odoo pod clones it from GitHub at startup via the `initContainer`. To pick up addon changes:

```bash
# Restart the admin Odoo pod to trigger a fresh git clone
kubectl -n odoo-admin rollout restart deployment/odoo-admin
```

This can be added to the workflow if `odoo_k8s_saas/**` changed:

```yaml
- name: Restart admin Odoo on addon changes
  if: ${{ contains(github.event.commits[*].modified, 'odoo_k8s_saas/') }}
  uses: appleboy/ssh-action@v1
  with:
    host: ${{ secrets.K3S_HOST }}
    username: ${{ secrets.K3S_USER }}
    key: ${{ secrets.K3S_SSH_KEY }}
    script: |
      kubectl -n odoo-admin rollout restart deployment/odoo-admin
```

## Build Notes

### Portal Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN adduser --disabled-password --gecos "" portal && chown -R portal /app
USER portal
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

- Non-root `portal` user
- No build args — all configuration comes from K8s Secrets/env
- Single uvicorn worker (MVP — scale horizontally via Deployment replicas)
