# CI/CD Pipeline

The pipeline builds and publishes the portal Docker image from the `main` branch to GitHub Container Registry (GHCR).

> **Note:** Deployment to the K3s node is **manual** — the pipeline does NOT SSH into any server. After the image is pushed, a human must SSH in and restart the deployment. See the [Operational Runbook](Operational-Runbook) for the manual deploy steps.
>
> **Secrets management:** The pipeline **never** reads `.secrets.env` or `k8s/01-secrets.yaml`. Live cluster secrets are managed separately via `./infra/apply-manifests.sh` run by a human after initial provisioning (or on rotation).

## Pipeline Overview

```
git push → main
      │
      ▼
GitHub Actions  (.github/workflows/ci.yaml)
  ┌─────────────────────────────────────────────┐
  │  job: build-portal                           │
  │                                              │
  │  1. checkout                                 │
  │  2. docker buildx build portal/Dockerfile    │
  │  3. push → ghcr.io/.../portal:latest         │
  │             + portal:<sha>                   │
  └─────────────────────────────────────────────┘

  ⬇ (manual step — not in the workflow)
  SSH into K3s node → kubectl rollout restart deployment/portal
```

## Container Registry

| Field | Value |
|:---|:---|
| Registry | `ghcr.io` (GitHub Container Registry) |
| Image | `ghcr.io/aei-software/aei-odoo-saas/portal` |
| Tags | see table below |
| Pull policy | `Always` (set in deployment manifests) |

### Image tag mapping

| Tag | Branch | Used by | Purpose |
|:----|:-------|:--------|:--------|
| `:main` | `main` | `07-staging.yaml` (portal-stg) | Staging — updated on every push to main |
| `:18.0` | `18.0` | — | Branch-pinned production build |
| `:stable` | `18.0` | `05-portal.yaml` (portal prod) | Production — only updated from 18.0 branch |
| `:<git-sha>` | any | — | Immutable traceability tag per commit |

The portal Deployment uses `imagePullPolicy: Always`, so a `rollout restart` pulls the new image even if the tag is the same.

## Workflow YAML

The workflow lives at `.github/workflows/ci.yaml`:

```yaml
name: CI — K3s HA Stack

on:
  push:
    branches: [main, "18.0", "feature/*"]
  pull_request:
    branches: [main, "18.0", "feature/*"]

jobs:
  build-portal:
    name: Build & Push Portal
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to GHCR
        if: github.event_name == 'push'
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Sanitize ref name for Docker tag
        id: meta
        run: echo "tag=${GITHUB_REF_NAME//\//-}" >> "$GITHUB_OUTPUT"

      - name: Build and push (with layer cache)
        uses: docker/build-push-action@v6
        with:
          context: portal
          push: ${{ github.event_name == 'push' }}
          tags: |
            ghcr.io/${{ github.repository_owner }}/aei-odoo-saas/portal:${{ steps.meta.outputs.tag }}
            ghcr.io/${{ github.repository_owner }}/aei-odoo-saas/portal:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      # On 18.0 branch, also tag as :stable for production clarity
      - name: Tag stable for production
        if: github.event_name == 'push' && github.ref == 'refs/heads/18.0'
        uses: docker/build-push-action@v6
        with:
          context: portal
          push: true
          tags: |
            ghcr.io/${{ github.repository_owner }}/aei-odoo-saas/portal:stable
          cache-from: type=gha
```

## Required GitHub Secrets

| Secret | Description |
|:---|:---|
| `GITHUB_TOKEN` | Auto-provided by GitHub Actions — used for GHCR auth |

No additional secrets are required. The pipeline only builds and pushes — it does not deploy.

## Manual Deployment After Build

After the CI pipeline pushes a new image, SSH into the K3s node and restart the portal:

```bash
ssh user@<K3S_HOST>
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware rollout restart deployment/portal
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware rollout status deployment/portal --timeout=120s
```

## Odoo Addon Deployment

The `odoo_k8s_saas` addon is deployed differently — the admin Odoo pod clones it from GitHub at startup via the `initContainer`. To pick up addon changes:

```bash
# Restart the admin Odoo pod to trigger a fresh git clone
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n odoo-admin rollout restart deployment/odoo-admin
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

## Future: Automated SSH Deploy

To automate the deployment step, add an SSH action to the workflow:

```yaml
- name: Deploy to K3s node
  if: github.event_name == 'push' && github.ref == 'refs/heads/main'
  uses: appleboy/ssh-action@v1
  with:
    host: ${{ secrets.K3S_HOST }}
    username: ${{ secrets.K3S_USER }}
    key: ${{ secrets.K3S_SSH_KEY }}
    script: |
      KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware rollout restart deployment/portal
      KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n aeisoftware rollout status deployment/portal --timeout=120s
```

This would require adding `K3S_HOST`, `K3S_USER`, and `K3S_SSH_KEY` as GitHub Secrets.
