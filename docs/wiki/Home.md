# Odoo SaaS MVP — Wiki

Multi-node K3s platform (Ceph RBD storage) that hosts multiple Odoo 18 tenants with automated provisioning, per-user billing, and integrated QR payments.

**Launch date:** 2026-04-15 — www.aeisoftware.com

## Architecture

- [High-Level Design (HLD)](High-Level-Design-(HLD).md) — component map, routing, database, security
- [Low-Level Design (LLD)](Low-Level-Design-(LLD).md) — K8s resources, configs, env vars, probes
- [Production Cloud Environment](Production-Cloud-Environment.md) — Ceph RBD, PostgreSQL HA (Patroni + HAProxy), Cilium networking

## Components

- [Odoo SaaS Addon](Odoo-SaaS-Addon.md) — `odoo_k8s_saas` module: models, actions, cron, views
- [Sales Integration](Sales-Integration.md) — **quote-to-provision pipeline**: sale order → invoice → payment → auto-provision
- [Subscription Integration](Subscription-Integration.md) — **recurring billing**: subscription_oca + bridge module for SaaS lifecycle management, **customer portal** at `/my/subscriptions`, per-user billing
- [Payment QR Mercantil](Payment-QR-Mercantil.md) — QR payment provider for Banco Mercantil Santa Cruz (Bolivia)
- [Portal API Reference](Portal-API-Reference.md) — FastAPI endpoints for provisioning, status, deletion

## Operations

### Development Environment (Local K3s)
- [DAY0 Install From Scratch](DAY0-Install-From-Scratch.md) — full setup walkthrough for a fresh VM or standard K3s.
- [Local Deployment WSL](Local-Deployment-WSL.md) — `dev-setup.sh` for local K3s on WSL/Linux.

### Production Environment (Cloud K3s)
- [Production Cloud Environment](Production-Cloud-Environment.md) — architecture differences, storage (`ceph-rbd`), and Ceph-specific security context fixes in production.

### General
- [Secrets Management](Secrets-Management.md) — `.secrets.env` workflow, credential rotation, drift diagnosis.
- [PostgreSQL Cluster Operations](PostgreSQL-Cluster-Operations.md) — **referencia operativa del cluster Patroni**: topología, puertos, roles, eliminación de tenants, errores comunes, docs stale.
- [Operational Runbook](Operational-Runbook.md) — DAY1/DAY2 health checks, backups (pgBackRest), **monitoring stack (Prometheus+Grafana+Loki)**, debugging, scaling.
- [CICD Pipeline](CICD-Pipeline.md) — GitHub Actions build and push workflow (deploy is manual).
- [Branch Strategy and Promotion](Branch-Strategy-and-Promotion.md) — two-branch model (`main`=staging, `18.0`=production), code promotion procedure, rollback.

## Security & Audit

- [Auditoria Produccion](Auditoria-Produccion.md) — 18 hallazgos de seguridad/estabilidad, 6 bugs de código, estado de resolución
- [Roadmap Hardening](Roadmap-Hardening.md) — plan de implementación en 4 fases — **27/30 items completados** (2026-04-16)

### Security Features (implemented)
| Feature | Description |
|:--------|:-----------|
| SQL Injection Protection | DDL queries use `psycopg2.sql.Identifier()` instead of f-strings |
| NetworkPolicy | Default-deny + whitelist for `odoo-admin` namespace; per-tenant isolation |
| PodDisruptionBudgets | `minAvailable: 1` for portal and odoo-admin during node maintenance |
| Liveness Probes | All services (portal, odoo-admin, staging, tenants) have health checks |
| Tenant ID Validation | Regex + min 2 chars + SQL UNIQUE constraint |
| Secrets Management | Passwords via K8s Secrets + init-container `render-config`, never in ConfigMaps |
| Image Pinning | `cloudflared:2026.3.0` (was `:latest`) |
| SCRAM Auth | PostgreSQL uses `scram-sha-256` authentication |
| pgBackRest Backups | Full (Sun) + Diff (Mon-Sat) + WAL archiving → Ceph S3, encrypted AES-256 |
| Monitoring Stack | Prometheus + Grafana + AlertManager + Loki + Promtail, 32 targets |

## Monitoring

Access via `https://grafana.aeisoftware.com` or port-forward:
```bash
kubectl -n monitoring port-forward svc/kube-prom-grafana 3000:80
# → admin / AeiMonitor2026
```

| Component | Function |
|:----------|:---------|
| Prometheus | Metrics collection (15d retention, 20Gi storage) |
| Grafana | Dashboards & visualization (Prometheus + Loki datasources) |
| Loki + Promtail | Centralized log aggregation from all K3s pods |
| AlertManager | Alert routing (email, webhook) |
| postgres_exporter | PostgreSQL metrics (database sizes, connections, replication lag) |
| node_exporter | System metrics on K3s + PG HA nodes |

See [Operational Runbook](Operational-Runbook.md) for PromQL queries, LogQL examples, and troubleshooting.

See [Runbook: Backup and Restore](Runbook-Backup-and-Restore.md) for pgBackRest PITR, pg_dump single-tenant restore, filestore recovery, and restore drill log.

## Roadmap

- [Roadmap: Production Readiness 100 Tenants](Roadmap-Production-Readiness-100-Tenants.md) — _(BLOQUEANTE lanzamiento abril 2026)_ 10 hitos: capacidad, quotas, backups, HA
- [Roadmap Hardening](Roadmap-Hardening.md) — _(PRIORITARIO)_ hardening de seguridad, bugs críticos, estabilidad para 100+ clientes — **27/30 items completados** (2026-04-16)
- [Roadmap: Audit v3 P0](Roadmap-Audit-v3-P0.md) — _(PENDIENTE)_ gaps contra "One Man SaaS Architecture": cloudflared pin, rotar password, Sentry, cron alerts, Trivy CI, smoke test e2e, backup offsite S3 (2026-04-17)
- *Roadmap: Infrastructure Analysis & Scaling* — resource limits, bottleneck analysis, capacity planning & multi-node scaling path (página nunca creada en la wiki original)

## QA & Testing

- [QA Testing Battery](QA-Testing-Battery.md) — batería de pruebas estructurada A-G para QA freeze (2026-04-16)

## Estado del Release

| Check | Estado |
|:------|:-------|
| C1 — RBAC `pods/exec` en manifiesto | ✅ Corregido 2026-04-16 |
| C2 — Campo `sale_subscription_id` en billing cron | ✅ Corregido 2026-04-16 |
| C3 — ACL `saas.instance` restringida a `group_system` | ✅ Corregido 2026-04-16 |
| C4 — Webhook env vars en manifiestos K8s | ✅ Ya configurado; patchar secret en runtime |
| C5 — Rollback en `POST /instances` ante fallo K8s | ✅ Corregido 2026-04-16 |
| QA freeze | 🔲 Pendiente — arrancar con [QA Testing Battery](QA-Testing-Battery.md) |
| Merge `main` → `18.0` | 🔲 Pendiente — después de QA green |

> Secrets runtime (C4): ejecutar `kubectl patch secret portal-secret` con `SAAS_WEBHOOK_KEY` en namespaces `staging`, `odoo-admin`, `aeisoftware` antes de iniciar QA.
